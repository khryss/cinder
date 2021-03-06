# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
# Copyright (c) 2014 TrilioData, Inc
# Copyright (c) 2015 EMC Corporation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Handles all requests relating to the volume backups service.
"""

from datetime import datetime
from eventlet import greenthread
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import strutils
from pytz import timezone
import random

from cinder.backup import rpcapi as backup_rpcapi
from cinder import context
from cinder.db import base
from cinder import exception
from cinder.i18n import _, _LI, _LW
from cinder import objects
from cinder.objects import fields
import cinder.policy
from cinder import quota
from cinder import utils
import cinder.volume

backup_api_opts = [
    cfg.BoolOpt('backup_use_same_backend',
                default=False,
                help='Backup services use same backend.')
]

CONF = cfg.CONF
CONF.register_opts(backup_api_opts)
LOG = logging.getLogger(__name__)
QUOTAS = quota.QUOTAS


def check_policy(context, action):
    target = {
        'project_id': context.project_id,
        'user_id': context.user_id,
    }
    _action = 'backup:%s' % action
    cinder.policy.enforce(context, _action, target)


class API(base.Base):
    """API for interacting with the volume backup manager."""

    def __init__(self, db_driver=None):
        self.backup_rpcapi = backup_rpcapi.BackupAPI()
        self.volume_api = cinder.volume.API()
        super(API, self).__init__(db_driver)

    def get(self, context, backup_id):
        check_policy(context, 'get')
        return objects.Backup.get_by_id(context, backup_id)

    def _check_support_to_force_delete(self, context, backup_host):
        result = self.backup_rpcapi.check_support_to_force_delete(context,
                                                                  backup_host)
        return result

    def delete(self, context, backup, force=False):
        """Make the RPC call to delete a volume backup.

        Call backup manager to execute backup delete or force delete operation.
        :param context: running context
        :param backup: the dict of backup that is got from DB.
        :param force: indicate force delete or not
        :raises: InvalidBackup
        :raises: BackupDriverException
        :raises: ServiceNotFound
        """
        check_policy(context, 'delete')
        if not force and backup.status not in [fields.BackupStatus.AVAILABLE,
                                               fields.BackupStatus.ERROR]:
            msg = _('Backup status must be available or error')
            raise exception.InvalidBackup(reason=msg)
        if force and not self._check_support_to_force_delete(context,
                                                             backup.host):
            msg = _('force delete')
            raise exception.NotSupportedOperation(operation=msg)

        # Don't allow backup to be deleted if there are incremental
        # backups dependent on it.
        deltas = self.get_all(context, search_opts={'parent_id': backup.id})
        if deltas and len(deltas):
            msg = _('Incremental backups exist for this backup.')
            raise exception.InvalidBackup(reason=msg)

        backup.status = fields.BackupStatus.DELETING
        backup.host = self._get_available_backup_service_host(
            backup.host, backup.availability_zone)
        backup.save()
        self.backup_rpcapi.delete_backup(context, backup)

    def get_all(self, context, search_opts=None, marker=None, limit=None,
                offset=None, sort_keys=None, sort_dirs=None):
        check_policy(context, 'get_all')

        search_opts = search_opts or {}

        all_tenants = search_opts.pop('all_tenants', '0')
        if not utils.is_valid_boolstr(all_tenants):
            msg = _("all_tenants must be a boolean, got '%s'.") % all_tenants
            raise exception.InvalidParameterValue(err=msg)

        if context.is_admin and strutils.bool_from_string(all_tenants):
            backups = objects.BackupList.get_all(context, search_opts,
                                                 marker, limit, offset,
                                                 sort_keys, sort_dirs)
        else:
            backups = objects.BackupList.get_all_by_project(
                context, context.project_id, search_opts,
                marker, limit, offset, sort_keys, sort_dirs
            )

        return backups

    def _az_matched(self, service, availability_zone):
        return ((not availability_zone) or
                service.availability_zone == availability_zone)

    def _is_backup_service_enabled(self, availability_zone, host):
        """Check if there is a backup service available."""
        topic = CONF.backup_topic
        ctxt = context.get_admin_context()
        services = objects.ServiceList.get_all_by_topic(
            ctxt, topic, disabled=False)
        for srv in services:
            if (self._az_matched(srv, availability_zone) and
                    srv.host == host and
                    utils.service_is_up(srv)):
                return True
        return False

    def _get_any_available_backup_service(self, availability_zone):
        """Get an available backup service host.

        Get an available backup service host in the specified
        availability zone.
        """
        services = [srv for srv in self._list_backup_services()]
        random.shuffle(services)
        # Get the next running service with matching availability zone.
        idx = 0
        while idx < len(services):
            srv = services[idx]
            if(self._az_matched(srv, availability_zone) and
               utils.service_is_up(srv)):
                return srv.host
            idx = idx + 1
        return None

    def _get_available_backup_service_host(self, host, availability_zone):
        """Return an appropriate backup service host."""
        backup_host = None
        if host and self._is_backup_service_enabled(availability_zone, host):
            backup_host = host
        if not backup_host and (not host or CONF.backup_use_same_backend):
            backup_host = self._get_any_available_backup_service(
                availability_zone)
        if not backup_host:
            raise exception.ServiceNotFound(service_id='cinder-backup')
        return backup_host

    def _list_backup_services(self):
        """List all enabled backup services.

        :returns: list -- hosts for services that are enabled for backup.
        """
        topic = CONF.backup_topic
        ctxt = context.get_admin_context()
        services = objects.ServiceList.get_all_by_topic(
            ctxt, topic, disabled=False)
        return services

    def _list_backup_hosts(self):
        services = self._list_backup_services()
        return [srv.host for srv in services
                if not srv.disabled and utils.service_is_up(srv)]

    def create(self, context, name, description, volume_id,
               container, incremental=False, availability_zone=None,
               force=False, snapshot_id=None):
        """Make the RPC call to create a volume backup."""
        check_policy(context, 'create')
        volume = self.volume_api.get(context, volume_id)
        snapshot = None
        if snapshot_id:
            snapshot = self.volume_api.get_snapshot(context, snapshot_id)

        if volume['status'] not in ["available", "in-use"]:
            msg = (_('Volume to be backed up must be available '
                     'or in-use, but the current status is "%s".')
                   % volume['status'])
            raise exception.InvalidVolume(reason=msg)
        elif volume['status'] in ["in-use"] and not snapshot_id and not force:
            msg = _('Backing up an in-use volume must use '
                    'the force flag.')
            raise exception.InvalidVolume(reason=msg)
        elif snapshot_id and snapshot['status'] not in ["available"]:
            msg = (_('Snapshot to be backed up must be available, '
                     'but the current status is "%s".')
                   % snapshot['status'])
            raise exception.InvalidSnapshot(reason=msg)

        previous_status = volume['status']
        host = self._get_available_backup_service_host(
            None, volume.availability_zone)

        # Reserve a quota before setting volume status and backup status
        try:
            reserve_opts = {'backups': 1,
                            'backup_gigabytes': volume['size']}
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except exception.OverQuota as e:
            overs = e.kwargs['overs']
            usages = e.kwargs['usages']
            quotas = e.kwargs['quotas']

            def _consumed(resource_name):
                return (usages[resource_name]['reserved'] +
                        usages[resource_name]['in_use'])

            for over in overs:
                if 'gigabytes' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "%(s_size)sG backup (%(d_consumed)dG of "
                              "%(d_quota)dG already consumed)")
                    LOG.warning(msg, {'s_pid': context.project_id,
                                      's_size': volume['size'],
                                      'd_consumed': _consumed(over),
                                      'd_quota': quotas[over]})
                    raise exception.VolumeBackupSizeExceedsAvailableQuota(
                        requested=volume['size'],
                        consumed=_consumed('backup_gigabytes'),
                        quota=quotas['backup_gigabytes'])
                elif 'backups' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "backups (%(d_consumed)d backups "
                              "already consumed)")

                    LOG.warning(msg, {'s_pid': context.project_id,
                                      'd_consumed': _consumed(over)})
                    raise exception.BackupLimitExceeded(
                        allowed=quotas[over])

        # Find the latest backup and use it as the parent backup to do an
        # incremental backup.
        latest_backup = None
        if incremental:
            backups = objects.BackupList.get_all_by_volume(context.elevated(),
                                                           volume_id)
            if backups.objects:
                # NOTE(xyang): The 'data_timestamp' field records the time
                # when the data on the volume was first saved. If it is
                # a backup from volume, 'data_timestamp' will be the same
                # as 'created_at' for a backup. If it is a backup from a
                # snapshot, 'data_timestamp' will be the same as
                # 'created_at' for a snapshot.
                # If not backing up from snapshot, the backup with the latest
                # 'data_timestamp' will be the parent; If backing up from
                # snapshot, the backup with the latest 'data_timestamp' will
                # be chosen only if 'data_timestamp' is earlier than the
                # 'created_at' timestamp of the snapshot; Otherwise, the
                # backup will not be chosen as the parent.
                # For example, a volume has a backup taken at 8:00, then
                # a snapshot taken at 8:10, and then a backup at 8:20.
                # When taking an incremental backup of the snapshot, the
                # parent should be the backup at 8:00, not 8:20, and the
                # 'data_timestamp' of this new backup will be 8:10.
                latest_backup = max(
                    backups.objects,
                    key=lambda x: x['data_timestamp']
                    if (not snapshot or (snapshot and x['data_timestamp']
                                         < snapshot['created_at']))
                    else datetime(1, 1, 1, 1, 1, 1, tzinfo=timezone('UTC')))
            else:
                msg = _('No backups available to do an incremental backup.')
                raise exception.InvalidBackup(reason=msg)

        parent_id = None
        if latest_backup:
            parent_id = latest_backup.id
            if latest_backup['status'] != fields.BackupStatus.AVAILABLE:
                msg = _('The parent backup must be available for '
                        'incremental backup.')
                raise exception.InvalidBackup(reason=msg)

        data_timestamp = None
        if snapshot_id:
            snapshot = objects.Snapshot.get_by_id(context, snapshot_id)
            data_timestamp = snapshot.created_at

        self.db.volume_update(context, volume_id,
                              {'status': 'backing-up',
                               'previous_status': previous_status})

        backup = None
        try:
            kwargs = {
                'user_id': context.user_id,
                'project_id': context.project_id,
                'display_name': name,
                'display_description': description,
                'volume_id': volume_id,
                'status': fields.BackupStatus.CREATING,
                'container': container,
                'parent_id': parent_id,
                'size': volume['size'],
                'host': host,
                'snapshot_id': snapshot_id,
                'data_timestamp': data_timestamp,
            }
            backup = objects.Backup(context=context, **kwargs)
            backup.create()
            if not snapshot_id:
                backup.data_timestamp = backup.created_at
                backup.save()
            QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    if backup and 'id' in backup:
                        backup.destroy()
                finally:
                    QUOTAS.rollback(context, reservations)

        # TODO(DuncanT): In future, when we have a generic local attach,
        #                this can go via the scheduler, which enables
        #                better load balancing and isolation of services
        self.backup_rpcapi.create_backup(context, backup)

        return backup

    def restore(self, context, backup_id, volume_id=None, name=None):
        """Make the RPC call to restore a volume backup."""
        check_policy(context, 'restore')
        backup = self.get(context, backup_id)
        if backup['status'] != fields.BackupStatus.AVAILABLE:
            msg = _('Backup status must be available')
            raise exception.InvalidBackup(reason=msg)

        size = backup['size']
        if size is None:
            msg = _('Backup to be restored has invalid size')
            raise exception.InvalidBackup(reason=msg)

        # Create a volume if none specified. If a volume is specified check
        # it is large enough for the backup
        if volume_id is None:
            if name is None:
                name = 'restore_backup_%s' % backup_id

            description = 'auto-created_from_restore_from_backup'

            LOG.info(_LI("Creating volume of %(size)s GB for restore of "
                         "backup %(backup_id)s."),
                     {'size': size, 'backup_id': backup_id},
                     context=context)
            volume = self.volume_api.create(context, size, name, description)
            volume_id = volume['id']

            while True:
                volume = self.volume_api.get(context, volume_id)
                if volume['status'] != 'creating':
                    break
                greenthread.sleep(1)
        else:
            volume = self.volume_api.get(context, volume_id)

        if volume['status'] != "available":
            msg = _('Volume to be restored to must be available')
            raise exception.InvalidVolume(reason=msg)

        LOG.debug('Checking backup size %(bs)s against volume size %(vs)s',
                  {'bs': size, 'vs': volume['size']})
        if size > volume['size']:
            msg = (_('volume size %(volume_size)d is too small to restore '
                     'backup of size %(size)d.') %
                   {'volume_size': volume['size'], 'size': size})
            raise exception.InvalidVolume(reason=msg)

        LOG.info(_LI("Overwriting volume %(volume_id)s with restore of "
                     "backup %(backup_id)s"),
                 {'volume_id': volume_id, 'backup_id': backup_id},
                 context=context)

        # Setting the status here rather than setting at start and unrolling
        # for each error condition, it should be a very small window
        backup.host = self._get_available_backup_service_host(
            backup.host, backup.availability_zone)
        backup.status = fields.BackupStatus.RESTORING
        backup.restore_volume_id = volume.id
        backup.save()
        self.db.volume_update(context, volume_id, {'status':
                                                   'restoring-backup'})

        self.backup_rpcapi.restore_backup(context, backup.host, backup,
                                          volume_id)

        d = {'backup_id': backup_id,
             'volume_id': volume_id,
             'volume_name': volume['display_name'], }

        return d

    def reset_status(self, context, backup_id, status):
        """Make the RPC call to reset a volume backup's status.

        Call backup manager to execute backup status reset operation.
        :param context: running context
        :param backup_id: which backup's status to be reset
        :parma status: backup's status to be reset
        :raises: InvalidBackup
        """
        # get backup info
        backup = self.get(context, backup_id)
        backup.host = self._get_available_backup_service_host(
            backup.host, backup.availability_zone)
        backup.save()
        # send to manager to do reset operation
        self.backup_rpcapi.reset_status(ctxt=context, backup=backup,
                                        status=status)

    def export_record(self, context, backup_id):
        """Make the RPC call to export a volume backup.

        Call backup manager to execute backup export.

        :param context: running context
        :param backup_id: backup id to export
        :returns: dictionary -- a description of how to import the backup
        :returns: contains 'backup_url' and 'backup_service'
        :raises: InvalidBackup
        """
        check_policy(context, 'backup-export')
        backup = self.get(context, backup_id)
        if backup['status'] != fields.BackupStatus.AVAILABLE:
            msg = (_('Backup status must be available and not %s.') %
                   backup['status'])
            raise exception.InvalidBackup(reason=msg)

        LOG.debug("Calling RPCAPI with context: "
                  "%(ctx)s, host: %(host)s, backup: %(id)s.",
                  {'ctx': context,
                   'host': backup['host'],
                   'id': backup['id']})

        backup.host = self._get_available_backup_service_host(
            backup.host, backup.availability_zone)
        backup.save()
        export_data = self.backup_rpcapi.export_record(context, backup)

        return export_data

    def _get_import_backup(self, context, backup_url):
        """Prepare database backup record for import.

        This method decodes provided backup_url and expects to find the id of
        the backup in there.

        Then checks the DB for the presence of this backup record and if it
        finds it and is not deleted it will raise an exception because the
        record cannot be created or used.

        If the record is in deleted status then we must be trying to recover
        this record, so we'll reuse it.

        If the record doesn't already exist we create it with provided id.

        :param context: running context
        :param backup_url: backup description to be used by the backup driver
        :return: BackupImport object
        :raises: InvalidBackup
        :raises: InvalidInput
        """
        # Deserialize string backup record into a dictionary
        backup_record = objects.Backup.decode_record(backup_url)

        # ID is a required field since it's what links incremental backups
        if 'id' not in backup_record:
            msg = _('Provided backup record is missing an id')
            raise exception.InvalidInput(reason=msg)

        kwargs = {
            'user_id': context.user_id,
            'project_id': context.project_id,
            'volume_id': '0000-0000-0000-0000',
            'status': fields.BackupStatus.CREATING,
        }

        try:
            # Try to get the backup with that ID in all projects even among
            # deleted entries.
            backup = objects.BackupImport.get_by_id(context,
                                                    backup_record['id'],
                                                    read_deleted='yes',
                                                    project_only=False)

            # If record exists and it's not deleted we cannot proceed with the
            # import
            if backup.status != fields.BackupStatus.DELETED:
                msg = _('Backup already exists in database.')
                raise exception.InvalidBackup(reason=msg)

            # Otherwise we'll "revive" delete backup record
            backup.update(kwargs)
            backup.save()

        except exception.BackupNotFound:
            # If record doesn't exist create it with the specific ID
            backup = objects.BackupImport(context=context,
                                          id=backup_record['id'], **kwargs)
            backup.create()

        return backup

    def import_record(self, context, backup_service, backup_url):
        """Make the RPC call to import a volume backup.

        :param context: running context
        :param backup_service: backup service name
        :param backup_url: backup description to be used by the backup driver
        :raises: InvalidBackup
        :raises: ServiceNotFound
        :raises: InvalidInput
        """
        check_policy(context, 'backup-import')

        # NOTE(ronenkat): since we don't have a backup-scheduler
        # we need to find a host that support the backup service
        # that was used to create the backup.
        # We  send it to the first backup service host, and the backup manager
        # on that host will forward it to other hosts on the hosts list if it
        # cannot support correct service itself.
        hosts = self._list_backup_hosts()
        if len(hosts) == 0:
            raise exception.ServiceNotFound(service_id=backup_service)

        # Get Backup object that will be used to import this backup record
        backup = self._get_import_backup(context, backup_url)

        first_host = hosts.pop()
        self.backup_rpcapi.import_record(context,
                                         first_host,
                                         backup,
                                         backup_service,
                                         backup_url,
                                         hosts)

        return backup
