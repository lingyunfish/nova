# vim: tabstop=4 shiftwidth=4 softtabstop=4
# encoding=UTF8

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""Unit tests for the DB API."""

import datetime
import uuid as stdlib_uuid

from oslo.config import cfg
from sqlalchemy import MetaData
from sqlalchemy.schema import Table
from sqlalchemy.sql.expression import select

from nova import context
from nova import db
from nova import exception
from nova.openstack.common.db.sqlalchemy import session as db_session
from nova.openstack.common import timeutils
from nova import test
from nova.tests import matchers
from nova import utils


CONF = cfg.CONF
CONF.import_opt('reserved_host_memory_mb', 'nova.compute.resource_tracker')
CONF.import_opt('reserved_host_disk_mb', 'nova.compute.resource_tracker')

get_engine = db_session.get_engine
get_session = db_session.get_session


class DbApiTestCase(test.TestCase):
    def setUp(self):
        super(DbApiTestCase, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

    def create_instances_with_args(self, **kwargs):
        args = {'reservation_id': 'a', 'image_ref': 1, 'host': 'host1',
                'project_id': self.project_id, 'vm_state': 'fake'}
        if 'context' in kwargs:
            ctxt = kwargs.pop('context')
            args['project_id'] = ctxt.project_id
        else:
            ctxt = self.context
        args.update(kwargs)
        return db.instance_create(ctxt, args)

    def test_create_instance_unique_hostname(self):
        otherprojectcontext = context.RequestContext(self.user_id,
                                          "%s2" % self.project_id)

        self.create_instances_with_args(hostname='fake_name')

        # With scope 'global' any duplicate should fail, be it this project:
        self.flags(osapi_compute_unique_server_name_scope='global')
        self.assertRaises(exception.InstanceExists,
                          self.create_instances_with_args,
                          hostname='fake_name')

        # or another:
        self.assertRaises(exception.InstanceExists,
                          self.create_instances_with_args,
                          context=otherprojectcontext,
                          hostname='fake_name')

        # With scope 'project' a duplicate in the project should fail:
        self.flags(osapi_compute_unique_server_name_scope='project')
        self.assertRaises(exception.InstanceExists,
                          self.create_instances_with_args,
                          hostname='fake_name')

        # With scope 'project' a duplicate in a different project should work:
        self.flags(osapi_compute_unique_server_name_scope='project')
        self.create_instances_with_args(context=otherprojectcontext,
                                        hostname='fake_name')

        self.flags(osapi_compute_unique_server_name_scope=None)

    def test_ec2_ids_not_found_are_printable(self):
        def check_exc_format(method):
            try:
                method(self.context, 'fake')
            except exception.NotFound as exc:
                self.assertTrue('fake' in unicode(exc))

        check_exc_format(db.get_ec2_volume_id_by_uuid)
        check_exc_format(db.get_volume_uuid_by_ec2_id)
        check_exc_format(db.get_ec2_snapshot_id_by_uuid)
        check_exc_format(db.get_snapshot_uuid_by_ec2_id)
        check_exc_format(db.get_ec2_instance_id_by_uuid)
        check_exc_format(db.get_instance_uuid_by_ec2_id)

    def test_instance_get_all_by_filters(self):
        self.create_instances_with_args()
        self.create_instances_with_args()
        result = db.instance_get_all_by_filters(self.context, {})
        self.assertEqual(2, len(result))

    def test_instance_get_all_by_filters_regex(self):
        self.create_instances_with_args(display_name='test1')
        self.create_instances_with_args(display_name='teeeest2')
        self.create_instances_with_args(display_name='diff')
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': 't.*st.'})
        self.assertEqual(2, len(result))

    def test_instance_get_all_by_filters_regex_unsupported_db(self):
        # Ensure that the 'LIKE' operator is used for unsupported dbs.
        self.flags(sql_connection="notdb://")
        self.create_instances_with_args(display_name='test1')
        self.create_instances_with_args(display_name='test.*')
        self.create_instances_with_args(display_name='diff')
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': 'test.*'})
        self.assertEqual(1, len(result))
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': '%test%'})
        self.assertEqual(2, len(result))

    def test_instance_get_all_by_filters_metadata(self):
        self.create_instances_with_args(metadata={'foo': 'bar'})
        self.create_instances_with_args()
        result = db.instance_get_all_by_filters(self.context,
                                                {'metadata': {'foo': 'bar'}})
        self.assertEqual(1, len(result))

    def test_instance_get_all_by_filters_unicode_value(self):
        self.create_instances_with_args(display_name=u'test♥')
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': u'test'})
        self.assertEqual(1, len(result))

    def test_instance_get_all_by_filters_deleted(self):
        inst1 = self.create_instances_with_args()
        inst2 = self.create_instances_with_args(reservation_id='b')
        db.instance_destroy(self.context, inst1['uuid'])
        result = db.instance_get_all_by_filters(self.context, {})
        self.assertEqual(2, len(result))
        self.assertIn(inst1['id'], [result[0]['id'], result[1]['id']])
        self.assertIn(inst2['id'], [result[0]['id'], result[1]['id']])
        if inst1['id'] == result[0]['id']:
            self.assertTrue(result[0]['deleted'])
        else:
            self.assertTrue(result[1]['deleted'])

    def test_instance_get_all_by_filters_paginate(self):
        self.flags(sql_connection="notdb://")
        test1 = self.create_instances_with_args(display_name='test1')
        test2 = self.create_instances_with_args(display_name='test2')
        test3 = self.create_instances_with_args(display_name='test3')

        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': '%test%'},
                                                marker=None)
        self.assertEqual(3, len(result))
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': '%test%'},
                                                sort_dir="asc",
                                                marker=test1['uuid'])
        self.assertEqual(2, len(result))
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': '%test%'},
                                                sort_dir="asc",
                                                marker=test2['uuid'])
        self.assertEqual(1, len(result))
        result = db.instance_get_all_by_filters(self.context,
                                                {'display_name': '%test%'},
                                                sort_dir="asc",
                                                marker=test3['uuid'])
        self.assertEqual(0, len(result))

        self.assertRaises(exception.MarkerNotFound,
                          db.instance_get_all_by_filters,
                          self.context, {'display_name': '%test%'},
                          marker=str(stdlib_uuid.uuid4()))

    def test_migration_get_unconfirmed_by_dest_compute(self):
        ctxt = context.get_admin_context()

        # Ensure no migrations are returned.
        results = db.migration_get_unconfirmed_by_dest_compute(ctxt, 10,
                'fake_host')
        self.assertEqual(0, len(results))

        # Ensure no migrations are returned.
        results = db.migration_get_unconfirmed_by_dest_compute(ctxt, 10,
                'fake_host2')
        self.assertEqual(0, len(results))

        updated_at = datetime.datetime(2000, 01, 01, 12, 00, 00)
        values = {"status": "finished", "updated_at": updated_at,
                "dest_compute": "fake_host2"}
        migration = db.migration_create(ctxt, values)

        # Ensure different host is not returned
        results = db.migration_get_unconfirmed_by_dest_compute(ctxt, 10,
                'fake_host')
        self.assertEqual(0, len(results))

        # Ensure one migration older than 10 seconds is returned.
        results = db.migration_get_unconfirmed_by_dest_compute(ctxt, 10,
                'fake_host2')
        self.assertEqual(1, len(results))
        db.migration_update(ctxt, migration['id'], {"status": "CONFIRMED"})

        # Ensure the new migration is not returned.
        updated_at = timeutils.utcnow()
        values = {"status": "finished", "updated_at": updated_at,
                "dest_compute": "fake_host2"}
        migration = db.migration_create(ctxt, values)
        results = db.migration_get_unconfirmed_by_dest_compute(ctxt, 10,
                "fake_host2")
        self.assertEqual(0, len(results))
        db.migration_update(ctxt, migration['id'], {"status": "CONFIRMED"})

    def test_instance_get_all_hung_in_rebooting(self):
        ctxt = context.get_admin_context()

        # Ensure no instances are returned.
        results = db.instance_get_all_hung_in_rebooting(ctxt, 10)
        self.assertEqual(0, len(results))

        # Ensure one rebooting instance with updated_at older than 10 seconds
        # is returned.
        updated_at = datetime.datetime(2000, 01, 01, 12, 00, 00)
        values = {"task_state": "rebooting", "updated_at": updated_at}
        instance = db.instance_create(ctxt, values)
        results = db.instance_get_all_hung_in_rebooting(ctxt, 10)
        self.assertEqual(1, len(results))
        db.instance_update(ctxt, instance['uuid'], {"task_state": None})

        # Ensure the newly rebooted instance is not returned.
        updated_at = timeutils.utcnow()
        values = {"task_state": "rebooting", "updated_at": updated_at}
        instance = db.instance_create(ctxt, values)
        results = db.instance_get_all_hung_in_rebooting(ctxt, 10)
        self.assertEqual(0, len(results))
        db.instance_update(ctxt, instance['uuid'], {"task_state": None})

    def test_multi_associate_disassociate(self):
        ctxt = context.get_admin_context()
        values = {'address': 'floating'}
        floating = db.floating_ip_create(ctxt, values)
        values = {'address': 'fixed'}
        fixed = db.fixed_ip_create(ctxt, values)
        res = db.floating_ip_fixed_ip_associate(ctxt, floating, fixed, 'foo')
        self.assertEqual(res['address'], fixed)
        res = db.floating_ip_fixed_ip_associate(ctxt, floating, fixed, 'foo')
        self.assertEqual(res, None)
        res = db.floating_ip_disassociate(ctxt, floating)
        self.assertEqual(res['address'], fixed)
        res = db.floating_ip_disassociate(ctxt, floating)
        self.assertEqual(res, None)

    def test_fixed_ip_get_by_floating_address(self):
        ctxt = context.get_admin_context()
        values = {'address': 'fixed'}
        fixed = db.fixed_ip_create(ctxt, values)
        fixed_ip_ref = db.fixed_ip_get_by_address(ctxt, fixed)
        values = {'address': 'floating',
                  'fixed_ip_id': fixed_ip_ref['id']}
        floating = db.floating_ip_create(ctxt, values)
        fixed_ip_ref = db.fixed_ip_get_by_floating_address(ctxt, floating)
        self.assertEqual(fixed, fixed_ip_ref['address'])

    def test_floating_ip_get_by_fixed_address(self):
        ctxt = context.get_admin_context()
        values = {'address': 'fixed'}
        fixed = db.fixed_ip_create(ctxt, values)
        fixed_ip_ref = db.fixed_ip_get_by_address(ctxt, fixed)
        values = {'address': 'floating1',
                  'fixed_ip_id': fixed_ip_ref['id']}
        floating1 = db.floating_ip_create(ctxt, values)
        values = {'address': 'floating2',
                  'fixed_ip_id': fixed_ip_ref['id']}
        floating2 = db.floating_ip_create(ctxt, values)
        floating_ip_refs = db.floating_ip_get_by_fixed_address(ctxt, fixed)
        self.assertEqual(floating1, floating_ip_refs[0]['address'])
        self.assertEqual(floating2, floating_ip_refs[1]['address'])

    def test_network_create_safe(self):
        ctxt = context.get_admin_context()
        values = {'host': 'localhost', 'project_id': 'project1'}
        network = db.network_create_safe(ctxt, values)
        self.assertNotEqual(None, network['uuid'])
        self.assertEqual(36, len(network['uuid']))
        db_network = db.network_get(ctxt, network['id'])
        self.assertEqual(network['uuid'], db_network['uuid'])

    def test_network_delete_safe(self):
        ctxt = context.get_admin_context()
        values = {'host': 'localhost', 'project_id': 'project1'}
        network = db.network_create_safe(ctxt, values)
        db_network = db.network_get(ctxt, network['id'])
        values = {'network_id': network['id'], 'address': 'fake1'}
        address1 = db.fixed_ip_create(ctxt, values)
        values = {'network_id': network['id'],
                  'address': 'fake2',
                  'allocated': True}
        address2 = db.fixed_ip_create(ctxt, values)
        self.assertRaises(exception.NetworkInUse,
                          db.network_delete_safe, ctxt, network['id'])
        db.fixed_ip_update(ctxt, address2, {'allocated': False})
        network = db.network_delete_safe(ctxt, network['id'])
        self.assertRaises(exception.FixedIpNotFoundForAddress,
                          db.fixed_ip_get_by_address, ctxt, address1)
        ctxt = ctxt.elevated(read_deleted='yes')
        fixed_ip = db.fixed_ip_get_by_address(ctxt, address1)
        self.assertTrue(fixed_ip['deleted'])

    def test_network_create_with_duplicate_vlan(self):
        ctxt = context.get_admin_context()
        values1 = {'host': 'localhost', 'project_id': 'project1', 'vlan': 1}
        values2 = {'host': 'something', 'project_id': 'project1', 'vlan': 1}
        db.network_create_safe(ctxt, values1)
        self.assertRaises(exception.DuplicateVlan,
                          db.network_create_safe, ctxt, values2)

    def test_network_update_with_duplicate_vlan(self):
        ctxt = context.get_admin_context()
        values1 = {'host': 'localhost', 'project_id': 'project1', 'vlan': 1}
        values2 = {'host': 'something', 'project_id': 'project1', 'vlan': 2}
        network_ref = db.network_create_safe(ctxt, values1)
        db.network_create_safe(ctxt, values2)
        self.assertRaises(exception.DuplicateVlan,
                          db.network_update,
                          ctxt, network_ref["id"], values2)

    def test_instance_update_with_instance_uuid(self):
        # test instance_update() works when an instance UUID is passed.
        ctxt = context.get_admin_context()

        # Create an instance with some metadata
        values = {'metadata': {'host': 'foo', 'key1': 'meow'},
                  'system_metadata': {'original_image_ref': 'blah'}}
        instance = db.instance_create(ctxt, values)

        # Update the metadata
        values = {'metadata': {'host': 'bar', 'key2': 'wuff'},
                  'system_metadata': {'original_image_ref': 'baz'}}
        db.instance_update(ctxt, instance['uuid'], values)

        # Retrieve the user-provided metadata to ensure it was successfully
        # updated
        instance_meta = db.instance_metadata_get(ctxt, instance['uuid'])
        self.assertEqual('bar', instance_meta['host'])
        self.assertEqual('wuff', instance_meta['key2'])
        self.assertNotIn('key1', instance_meta)

        # Retrieve the system metadata to ensure it was successfully updated
        system_meta = db.instance_system_metadata_get(ctxt, instance['uuid'])
        self.assertEqual('baz', system_meta['original_image_ref'])

    def test_instance_update_of_instance_type_id(self):
        ctxt = context.get_admin_context()

        inst_type1 = db.instance_type_get_by_name(ctxt, 'm1.tiny')
        inst_type2 = db.instance_type_get_by_name(ctxt, 'm1.small')

        values = {'instance_type_id': inst_type1['id']}
        instance = db.instance_create(ctxt, values)

        self.assertEqual(instance['instance_type']['id'], inst_type1['id'])
        self.assertEqual(instance['instance_type']['name'],
                inst_type1['name'])

        values = {'instance_type_id': inst_type2['id']}
        instance = db.instance_update(ctxt, instance['uuid'], values)

        self.assertEqual(instance['instance_type']['id'], inst_type2['id'])
        self.assertEqual(instance['instance_type']['name'],
                inst_type2['name'])

    def test_instance_update_unique_name(self):
        otherprojectcontext = context.RequestContext(self.user_id,
                                          "%s2" % self.project_id)

        inst = self.create_instances_with_args(hostname='fake_name')
        uuid1p1 = inst['uuid']
        inst = self.create_instances_with_args(hostname='fake_name2')
        uuid2p1 = inst['uuid']

        inst = self.create_instances_with_args(context=otherprojectcontext,
                                               hostname='fake_name3')
        uuid1p2 = inst['uuid']

        # osapi_compute_unique_server_name_scope is unset so this should work:
        values = {'hostname': 'fake_name2'}
        db.instance_update(self.context, uuid1p1, values)
        values = {'hostname': 'fake_name'}
        db.instance_update(self.context, uuid1p1, values)

        # With scope 'global' any duplicate should fail.
        self.flags(osapi_compute_unique_server_name_scope='global')
        self.assertRaises(exception.InstanceExists,
                          db.instance_update,
                          self.context,
                          uuid2p1,
                          values)

        self.assertRaises(exception.InstanceExists,
                          db.instance_update,
                          otherprojectcontext,
                          uuid1p2,
                          values)

        # But we should definitely be able to update our name if we aren't
        #  really changing it.
        case_only_values = {'hostname': 'fake_NAME'}
        db.instance_update(self.context, uuid1p1, case_only_values)

        # With scope 'project' a duplicate in the project should fail:
        self.flags(osapi_compute_unique_server_name_scope='project')
        self.assertRaises(exception.InstanceExists,
                          db.instance_update,
                          self.context,
                          uuid2p1,
                          values)

        # With scope 'project' a duplicate in a different project should work:
        self.flags(osapi_compute_unique_server_name_scope='project')
        db.instance_update(otherprojectcontext, uuid1p2, values)

    def test_instance_update_with_and_get_original(self):
        ctxt = context.get_admin_context()

        # Create an instance with some metadata
        values = {'vm_state': 'building'}
        instance = db.instance_create(ctxt, values)

        (old_ref, new_ref) = db.instance_update_and_get_original(ctxt,
                instance['uuid'], {'vm_state': 'needscoffee'})
        self.assertEquals("building", old_ref["vm_state"])
        self.assertEquals("needscoffee", new_ref["vm_state"])

    def test_instance_update_with_extra_specs(self):
        # Ensure _extra_specs are returned from _instance_update.
        ctxt = context.get_admin_context()

        # create a flavor
        inst_type_dict = dict(
                    name="test_flavor",
                    memory_mb=1,
                    vcpus=1,
                    root_gb=1,
                    ephemeral_gb=1,
                    flavorid=105)
        inst_type_ref = db.instance_type_create(ctxt, inst_type_dict)

        # add some extra spec to our flavor
        spec = {'test_spec': 'foo'}
        db.instance_type_extra_specs_update_or_create(
                    ctxt,
                    inst_type_ref['flavorid'],
                    spec)

        # create instance, just populates db, doesn't pull extra_spec
        instance = db.instance_create(
                    ctxt,
                    {'instance_type_id': inst_type_ref['id']})
        self.assertNotIn('extra_specs', instance)

        # update instance, used when starting instance to set state, etc
        (old_ref, new_ref) = db.instance_update_and_get_original(
                    ctxt,
                    instance['uuid'],
                    {})
        self.assertEquals(spec, old_ref['extra_specs'])
        self.assertEquals(spec, new_ref['extra_specs'])

    def _test_instance_update_updates_metadata(self, metadata_type):
        ctxt = context.get_admin_context()

        instance = db.instance_create(ctxt, {})

        def set_and_check(meta):
            inst = db.instance_update(ctxt, instance['uuid'],
                               {metadata_type: dict(meta)})
            _meta = utils.metadata_to_dict(inst[metadata_type])
            self.assertEqual(meta, _meta)

        meta = {'speed': '88', 'units': 'MPH'}
        set_and_check(meta)

        meta['gigawatts'] = '1.21'
        set_and_check(meta)

        del meta['gigawatts']
        set_and_check(meta)

    def test_instance_update_updates_system_metadata(self):
        # Ensure that system_metadata is updated during instance_update
        self._test_instance_update_updates_metadata('system_metadata')

    def test_instance_update_updates_metadata(self):
        # Ensure that metadata is updated during instance_update
        self._test_instance_update_updates_metadata('metadata')

    def test_instance_fault_create(self):
        # Ensure we can create an instance fault.
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        # Create a fault
        fault_values = {
            'message': 'message',
            'details': 'detail',
            'instance_uuid': uuid,
            'code': 404,
        }
        db.instance_fault_create(ctxt, fault_values)

        # Retrieve the fault to ensure it was successfully added
        faults = db.instance_fault_get_by_instance_uuids(ctxt, [uuid])
        self.assertEqual(404, faults[uuid][0]['code'])

    def test_instance_fault_get_by_instance(self):
        # ensure we can retrieve an instance fault by  instance UUID.
        ctxt = context.get_admin_context()
        instance1 = db.instance_create(ctxt, {})
        instance2 = db.instance_create(ctxt, {})
        uuids = [instance1['uuid'], instance2['uuid']]

        # Create faults
        fault_values = {
            'message': 'message',
            'details': 'detail',
            'instance_uuid': uuids[0],
            'code': 404,
        }
        fault1 = db.instance_fault_create(ctxt, fault_values)

        fault_values = {
            'message': 'message',
            'details': 'detail',
            'instance_uuid': uuids[0],
            'code': 500,
        }
        fault2 = db.instance_fault_create(ctxt, fault_values)

        fault_values = {
            'message': 'message',
            'details': 'detail',
            'instance_uuid': uuids[1],
            'code': 404,
        }
        fault3 = db.instance_fault_create(ctxt, fault_values)

        fault_values = {
            'message': 'message',
            'details': 'detail',
            'instance_uuid': uuids[1],
            'code': 500,
        }
        fault4 = db.instance_fault_create(ctxt, fault_values)

        instance_faults = db.instance_fault_get_by_instance_uuids(ctxt, uuids)

        expected = {
                uuids[0]: [fault2, fault1],
                uuids[1]: [fault4, fault3],
        }

        self.assertEqual(instance_faults, expected)

    def test_instance_faults_get_by_instance_uuids_no_faults(self):
        # None should be returned when no faults exist.
        ctxt = context.get_admin_context()
        instance1 = db.instance_create(ctxt, {})
        instance2 = db.instance_create(ctxt, {})
        uuids = [instance1['uuid'], instance2['uuid']]
        instance_faults = db.instance_fault_get_by_instance_uuids(ctxt, uuids)
        expected = {uuids[0]: [], uuids[1]: []}
        self.assertEqual(expected, instance_faults)

    def test_instance_action_start(self):
        """Create an instance action."""
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        start_time = timeutils.utcnow()
        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid,
                         'request_id': ctxt.request_id,
                         'user_id': ctxt.user_id,
                         'project_id': ctxt.project_id,
                         'start_time': start_time}
        db.action_start(ctxt, action_values)

        # Retrieve the action to ensure it was successfully added
        actions = db.actions_get(ctxt, uuid)
        self.assertEqual(1, len(actions))
        self.assertEqual('run_instance', actions[0]['action'])
        self.assertEqual(start_time, actions[0]['start_time'])
        self.assertEqual(ctxt.request_id, actions[0]['request_id'])
        self.assertEqual(ctxt.user_id, actions[0]['user_id'])
        self.assertEqual(ctxt.project_id, actions[0]['project_id'])

    def test_instance_action_finish(self):
        """Create an instance action."""
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        start_time = timeutils.utcnow()
        action_start_values = {'action': 'run_instance',
                               'instance_uuid': uuid,
                               'request_id': ctxt.request_id,
                               'user_id': ctxt.user_id,
                               'project_id': ctxt.project_id,
                               'start_time': start_time}
        db.action_start(ctxt, action_start_values)

        finish_time = timeutils.utcnow() + datetime.timedelta(seconds=5)
        action_finish_values = {'instance_uuid': uuid,
                                'request_id': ctxt.request_id,
                                'finish_time': finish_time}
        db.action_finish(ctxt, action_finish_values)

        # Retrieve the action to ensure it was successfully added
        actions = db.actions_get(ctxt, uuid)
        self.assertEqual(1, len(actions))
        self.assertEqual('run_instance', actions[0]['action'])
        self.assertEqual(start_time, actions[0]['start_time'])
        self.assertEqual(finish_time, actions[0]['finish_time'])
        self.assertEqual(ctxt.request_id, actions[0]['request_id'])
        self.assertEqual(ctxt.user_id, actions[0]['user_id'])
        self.assertEqual(ctxt.project_id, actions[0]['project_id'])

    def test_instance_actions_get_by_instance(self):
        """Ensure we can get actions by UUID."""
        ctxt1 = context.get_admin_context()
        ctxt2 = context.get_admin_context()
        uuid1 = str(stdlib_uuid.uuid4())
        uuid2 = str(stdlib_uuid.uuid4())

        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid1,
                         'request_id': ctxt1.request_id,
                         'user_id': ctxt1.user_id,
                         'project_id': ctxt1.project_id,
                         'start_time': timeutils.utcnow()}
        db.action_start(ctxt1, action_values)
        action_values['action'] = 'resize'
        db.action_start(ctxt1, action_values)

        action_values = {'action': 'reboot',
                         'instance_uuid': uuid2,
                         'request_id': ctxt2.request_id,
                         'user_id': ctxt2.user_id,
                         'project_id': ctxt2.project_id,
                         'start_time': timeutils.utcnow()}
        db.action_start(ctxt2, action_values)
        db.action_start(ctxt2, action_values)

        # Retrieve the action to ensure it was successfully added
        actions = db.actions_get(ctxt1, uuid1)
        self.assertEqual(2, len(actions))
        self.assertEqual('resize', actions[0]['action'])
        self.assertEqual('run_instance', actions[1]['action'])

    def test_instance_action_get_by_instance_and_action(self):
        """Ensure we can get an action by instance UUID and action id."""
        ctxt1 = context.get_admin_context()
        ctxt2 = context.get_admin_context()
        uuid1 = str(stdlib_uuid.uuid4())
        uuid2 = str(stdlib_uuid.uuid4())

        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid1,
                         'request_id': ctxt1.request_id,
                         'user_id': ctxt1.user_id,
                         'project_id': ctxt1.project_id,
                         'start_time': timeutils.utcnow()}
        db.action_start(ctxt1, action_values)
        action_values['action'] = 'resize'
        db.action_start(ctxt1, action_values)

        action_values = {'action': 'reboot',
                         'instance_uuid': uuid2,
                         'request_id': ctxt2.request_id,
                         'user_id': ctxt2.user_id,
                         'project_id': ctxt2.project_id,
                         'start_time': timeutils.utcnow()}
        db.action_start(ctxt2, action_values)
        db.action_start(ctxt2, action_values)

        actions = db.actions_get(ctxt1, uuid1)
        request_id = actions[0]['request_id']
        action = db.action_get_by_request_id(ctxt1, uuid1, request_id)
        self.assertEqual('run_instance', action['action'])
        self.assertEqual(ctxt1.request_id, action['request_id'])

    def test_instance_action_event_start(self):
        """Create an instance action event."""
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        start_time = timeutils.utcnow()
        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid,
                         'request_id': ctxt.request_id,
                         'user_id': ctxt.user_id,
                         'project_id': ctxt.project_id,
                         'start_time': start_time}
        action = db.action_start(ctxt, action_values)

        event_values = {'event': 'schedule',
                        'instance_uuid': uuid,
                        'request_id': ctxt.request_id,
                        'start_time': start_time}
        db.action_event_start(ctxt, event_values)

        # Retrieve the event to ensure it was successfully added
        events = db.action_events_get(ctxt, action['id'])
        self.assertEqual(1, len(events))
        self.assertEqual('schedule', events[0]['event'])
        self.assertEqual(start_time, events[0]['start_time'])

    def test_instance_action_event_finish(self):
        """Finish an instance action event."""
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        start_time = timeutils.utcnow()
        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid,
                         'request_id': ctxt.request_id,
                         'user_id': ctxt.user_id,
                         'project_id': ctxt.project_id,
                         'start_time': start_time}
        action = db.action_start(ctxt, action_values)

        event_values = {'event': 'schedule',
                        'request_id': ctxt.request_id,
                        'instance_uuid': uuid,
                        'start_time': start_time}
        db.action_event_start(ctxt, event_values)

        finish_time = timeutils.utcnow() + datetime.timedelta(seconds=5)
        event_finish_values = {'event': 'schedule',
                                'request_id': ctxt.request_id,
                                'instance_uuid': uuid,
                                'finish_time': finish_time}
        db.action_event_finish(ctxt, event_finish_values)

        # Retrieve the event to ensure it was successfully added
        events = db.action_events_get(ctxt, action['id'])
        self.assertEqual(1, len(events))
        self.assertEqual('schedule', events[0]['event'])
        self.assertEqual(start_time, events[0]['start_time'])
        self.assertEqual(finish_time, events[0]['finish_time'])

    def test_instance_action_and_event_start_string_time(self):
        """Create an instance action and event with a string start_time."""
        ctxt = context.get_admin_context()
        uuid = str(stdlib_uuid.uuid4())

        start_time = timeutils.utcnow()
        start_time_str = timeutils.strtime(start_time)
        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid,
                         'request_id': ctxt.request_id,
                         'user_id': ctxt.user_id,
                         'project_id': ctxt.project_id,
                         'start_time': start_time_str}
        action = db.action_start(ctxt, action_values)

        event_values = {'event': 'schedule',
                        'instance_uuid': uuid,
                        'request_id': ctxt.request_id,
                        'start_time': start_time_str}
        db.action_event_start(ctxt, event_values)

        # Retrieve the event to ensure it was successfully added
        events = db.action_events_get(ctxt, action['id'])
        self.assertEqual(1, len(events))
        self.assertEqual('schedule', events[0]['event'])
        # db api still returns models with datetime, not str, values
        self.assertEqual(start_time, events[0]['start_time'])

    def test_instance_action_event_get_by_id(self):
        """Get a specific instance action event."""
        ctxt1 = context.get_admin_context()
        ctxt2 = context.get_admin_context()
        uuid1 = str(stdlib_uuid.uuid4())
        uuid2 = str(stdlib_uuid.uuid4())

        action_values = {'action': 'run_instance',
                         'instance_uuid': uuid1,
                         'request_id': ctxt1.request_id,
                         'user_id': ctxt1.user_id,
                         'project_id': ctxt1.project_id,
                         'start_time': timeutils.utcnow()}
        added_action = db.action_start(ctxt1, action_values)

        action_values = {'action': 'reboot',
                         'instance_uuid': uuid2,
                         'request_id': ctxt2.request_id,
                         'user_id': ctxt2.user_id,
                         'project_id': ctxt2.project_id,
                         'start_time': timeutils.utcnow()}
        db.action_start(ctxt2, action_values)

        start_time = timeutils.utcnow()
        event_values = {'event': 'schedule',
                        'instance_uuid': uuid1,
                        'request_id': ctxt1.request_id,
                        'start_time': start_time}
        added_event = db.action_event_start(ctxt1, event_values)

        event_values = {'event': 'reboot',
                        'instance_uuid': uuid2,
                        'request_id': ctxt2.request_id,
                        'start_time': timeutils.utcnow()}
        db.action_event_start(ctxt2, event_values)

        # Retrieve the event to ensure it was successfully added
        event = db.action_event_get_by_id(ctxt1, added_action['id'],
                                                     added_event['id'])
        self.assertEqual('schedule', event['event'])
        self.assertEqual(start_time, event['start_time'])

    def test_dns_registration(self):
        domain1 = 'test.domain.one'
        domain2 = 'test.domain.two'
        testzone = 'testzone'
        ctxt = context.get_admin_context()

        db.dnsdomain_register_for_zone(ctxt, domain1, testzone)
        domain_ref = db.dnsdomain_get(ctxt, domain1)
        zone = domain_ref['availability_zone']
        scope = domain_ref['scope']
        self.assertEqual(scope, 'private')
        self.assertEqual(zone, testzone)

        db.dnsdomain_register_for_project(ctxt, domain2,
                                           self.project_id)
        domain_ref = db.dnsdomain_get(ctxt, domain2)
        project = domain_ref['project_id']
        scope = domain_ref['scope']
        self.assertEqual(project, self.project_id)
        self.assertEqual(scope, 'public')

        expected = [domain1, domain2]
        domains = db.dnsdomain_list(ctxt)
        self.assertEqual(expected, domains)

        db.dnsdomain_unregister(ctxt, domain1)
        db.dnsdomain_unregister(ctxt, domain2)

    def test_network_get_associated_fixed_ips(self):
        ctxt = context.get_admin_context()
        values = {'host': 'foo', 'hostname': 'myname'}
        instance = db.instance_create(ctxt, values)
        values = {'address': 'bar', 'instance_uuid': instance['uuid']}
        vif = db.virtual_interface_create(ctxt, values)
        values = {'address': 'baz',
                  'network_id': 1,
                  'allocated': True,
                  'instance_uuid': instance['uuid'],
                  'virtual_interface_id': vif['id']}
        fixed_address = db.fixed_ip_create(ctxt, values)
        data = db.network_get_associated_fixed_ips(ctxt, 1)
        self.assertEqual(len(data), 1)
        record = data[0]
        self.assertEqual(record['address'], fixed_address)
        self.assertEqual(record['instance_uuid'], instance['uuid'])
        self.assertEqual(record['network_id'], 1)
        self.assertEqual(record['instance_created'], instance['created_at'])
        self.assertEqual(record['instance_updated'], instance['updated_at'])
        self.assertEqual(record['instance_hostname'], instance['hostname'])
        self.assertEqual(record['vif_id'], vif['id'])
        self.assertEqual(record['vif_address'], vif['address'])
        data = db.network_get_associated_fixed_ips(ctxt, 1, 'nothing')
        self.assertEqual(len(data), 0)

    def test_network_get_all_by_host(self):
        ctxt = context.get_admin_context()
        data = db.network_get_all_by_host(ctxt, 'foo')
        self.assertEqual(len(data), 0)
        # dummy network
        net = db.network_create_safe(ctxt, {})
        # network with host set
        net = db.network_create_safe(ctxt, {'host': 'foo'})
        data = db.network_get_all_by_host(ctxt, 'foo')
        self.assertEqual(len(data), 1)
        # network with fixed ip with host set
        net = db.network_create_safe(ctxt, {})
        values = {'host': 'foo', 'network_id': net['id']}
        fixed_address = db.fixed_ip_create(ctxt, values)
        data = db.network_get_all_by_host(ctxt, 'foo')
        self.assertEqual(len(data), 2)
        # network with instance with host set
        net = db.network_create_safe(ctxt, {})
        instance = db.instance_create(ctxt, {'host': 'foo'})
        values = {'instance_uuid': instance['uuid']}
        vif = db.virtual_interface_create(ctxt, values)
        values = {'network_id': net['id'],
                  'virtual_interface_id': vif['id']}
        fixed_address = db.fixed_ip_create(ctxt, values)
        data = db.network_get_all_by_host(ctxt, 'foo')
        self.assertEqual(len(data), 3)

    def test_network_in_use_on_host(self):
        ctxt = context.get_admin_context()

        values = {'host': 'foo', 'hostname': 'myname'}
        instance = db.instance_create(ctxt, values)
        values = {'address': 'bar', 'instance_uuid': instance['uuid']}
        vif = db.virtual_interface_create(ctxt, values)
        values = {'address': 'baz',
                  'network_id': 1,
                  'allocated': True,
                  'instance_uuid': instance['uuid'],
                  'virtual_interface_id': vif['id']}
        db.fixed_ip_create(ctxt, values)

        self.assertEqual(db.network_in_use_on_host(ctxt, 1, 'foo'), True)
        self.assertEqual(db.network_in_use_on_host(ctxt, 1, 'bar'), False)

    def _timeout_test(self, ctxt, timeout, multi_host):
        values = {'host': 'foo'}
        instance = db.instance_create(ctxt, values)
        values = {'multi_host': multi_host, 'host': 'bar'}
        net = db.network_create_safe(ctxt, values)
        old = time = timeout - datetime.timedelta(seconds=5)
        new = time = timeout + datetime.timedelta(seconds=5)
        # should deallocate
        values = {'allocated': False,
                  'instance_uuid': instance['uuid'],
                  'network_id': net['id'],
                  'updated_at': old}
        db.fixed_ip_create(ctxt, values)
        # still allocated
        values = {'allocated': True,
                  'instance_uuid': instance['uuid'],
                  'network_id': net['id'],
                  'updated_at': old}
        db.fixed_ip_create(ctxt, values)
        # wrong network
        values = {'allocated': False,
                  'instance_uuid': instance['uuid'],
                  'network_id': None,
                  'updated_at': old}
        db.fixed_ip_create(ctxt, values)
        # too new
        values = {'allocated': False,
                  'instance_uuid': instance['uuid'],
                  'network_id': None,
                  'updated_at': new}
        db.fixed_ip_create(ctxt, values)

    def test_fixed_ip_disassociate_all_by_timeout_single_host(self):
        now = timeutils.utcnow()
        ctxt = context.get_admin_context()
        self._timeout_test(ctxt, now, False)
        result = db.fixed_ip_disassociate_all_by_timeout(ctxt, 'foo', now)
        self.assertEqual(result, 0)
        result = db.fixed_ip_disassociate_all_by_timeout(ctxt, 'bar', now)
        self.assertEqual(result, 1)

    def test_fixed_ip_disassociate_all_by_timeout_multi_host(self):
        now = timeutils.utcnow()
        ctxt = context.get_admin_context()
        self._timeout_test(ctxt, now, True)
        result = db.fixed_ip_disassociate_all_by_timeout(ctxt, 'foo', now)
        self.assertEqual(result, 1)
        result = db.fixed_ip_disassociate_all_by_timeout(ctxt, 'bar', now)
        self.assertEqual(result, 0)

    def test_get_vol_mapping_non_admin(self):
        ref = db.ec2_volume_create(self.context, 'fake-uuid')
        ec2_id = db.get_ec2_volume_id_by_uuid(self.context, 'fake-uuid')
        self.assertEqual(ref['id'], ec2_id)

    def test_get_snap_mapping_non_admin(self):
        ref = db.ec2_snapshot_create(self.context, 'fake-uuid')
        ec2_id = db.get_ec2_snapshot_id_by_uuid(self.context, 'fake-uuid')
        self.assertEqual(ref['id'], ec2_id)

    def test_bw_usage_calls(self):
        ctxt = context.get_admin_context()
        now = timeutils.utcnow()
        timeutils.set_time_override(now)
        start_period = now - datetime.timedelta(seconds=10)
        uuid3_refreshed = now - datetime.timedelta(seconds=5)

        expected_bw_usages = [{'uuid': 'fake_uuid1',
                               'mac': 'fake_mac1',
                               'start_period': start_period,
                               'bw_in': 100,
                               'bw_out': 200,
                               'last_ctr_in': 12345,
                               'last_ctr_out': 67890,
                               'last_refreshed': now},
                              {'uuid': 'fake_uuid2',
                               'mac': 'fake_mac2',
                               'start_period': start_period,
                               'bw_in': 200,
                               'bw_out': 300,
                               'last_ctr_in': 22345,
                               'last_ctr_out': 77890,
                               'last_refreshed': now},
                              {'uuid': 'fake_uuid3',
                               'mac': 'fake_mac3',
                               'start_period': start_period,
                               'bw_in': 400,
                               'bw_out': 500,
                               'last_ctr_in': 32345,
                               'last_ctr_out': 87890,
                               'last_refreshed': uuid3_refreshed}]

        def _compare(bw_usage, expected):
            for key, value in expected.items():
                self.assertEqual(bw_usage[key], value)

        bw_usages = db.bw_usage_get_by_uuids(ctxt,
                ['fake_uuid1', 'fake_uuid2'], start_period)
        # No matches
        self.assertEqual(len(bw_usages), 0)

        # Add 3 entries
        db.bw_usage_update(ctxt, 'fake_uuid1',
                'fake_mac1', start_period,
                100, 200, 12345, 67890)
        db.bw_usage_update(ctxt, 'fake_uuid2',
                'fake_mac2', start_period,
                100, 200, 42, 42)
        # Test explicit refreshed time
        db.bw_usage_update(ctxt, 'fake_uuid3',
                'fake_mac3', start_period,
                400, 500, 32345, 87890,
                last_refreshed=uuid3_refreshed)
        # Update 2nd entry
        db.bw_usage_update(ctxt, 'fake_uuid2',
                'fake_mac2', start_period,
                200, 300, 22345, 77890)

        bw_usages = db.bw_usage_get_by_uuids(ctxt,
                ['fake_uuid1', 'fake_uuid2', 'fake_uuid3'], start_period)
        self.assertEqual(len(bw_usages), 3)
        _compare(bw_usages[0], expected_bw_usages[0])
        _compare(bw_usages[1], expected_bw_usages[1])
        _compare(bw_usages[2], expected_bw_usages[2])
        timeutils.clear_time_override()


def _get_fake_aggr_values():
    return {'name': 'fake_aggregate'}


def _get_fake_aggr_metadata():
    return {'fake_key1': 'fake_value1',
            'fake_key2': 'fake_value2',
            'availability_zone': 'fake_avail_zone'}


def _get_fake_aggr_hosts():
    return ['foo.openstack.org']


def _create_aggregate(context=context.get_admin_context(),
                      values=_get_fake_aggr_values(),
                      metadata=_get_fake_aggr_metadata()):
    return db.aggregate_create(context, values, metadata)


def _create_aggregate_with_hosts(context=context.get_admin_context(),
                      values=_get_fake_aggr_values(),
                      metadata=_get_fake_aggr_metadata(),
                      hosts=_get_fake_aggr_hosts()):
    result = _create_aggregate(context=context,
                               values=values, metadata=metadata)
    for host in hosts:
        db.aggregate_host_add(context, result['id'], host)
    return result


class AggregateDBApiTestCase(test.TestCase):
    def setUp(self):
        super(AggregateDBApiTestCase, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

    def test_aggregate_create_no_metadata(self):
        result = _create_aggregate(metadata=None)
        self.assertEquals(result['name'], 'fake_aggregate')

    def test_aggregate_create_avoid_name_conflict(self):
        r1 = _create_aggregate(metadata=None)
        db.aggregate_delete(context.get_admin_context(), r1['id'])
        values = {'name': r1['name']}
        metadata = {'availability_zone': 'new_zone'}
        r2 = _create_aggregate(values=values, metadata=metadata)
        self.assertEqual(r2['name'], values['name'])
        self.assertEqual(r2['availability_zone'],
                metadata['availability_zone'])

    def test_aggregate_create_raise_exist_exc(self):
        _create_aggregate(metadata=None)
        self.assertRaises(exception.AggregateNameExists,
                          _create_aggregate, metadata=None)

    def test_aggregate_get_raise_not_found(self):
        ctxt = context.get_admin_context()
        # this does not exist!
        aggregate_id = 1
        self.assertRaises(exception.AggregateNotFound,
                          db.aggregate_get,
                          ctxt, aggregate_id)

    def test_aggregate_metadata_get_raise_not_found(self):
        ctxt = context.get_admin_context()
        # this does not exist!
        aggregate_id = 1
        self.assertRaises(exception.AggregateNotFound,
                          db.aggregate_metadata_get,
                          ctxt, aggregate_id)

    def test_aggregate_create_with_metadata(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        expected_metadata = db.aggregate_metadata_get(ctxt, result['id'])
        self.assertThat(expected_metadata,
                        matchers.DictMatches(_get_fake_aggr_metadata()))

    def test_aggregate_create_delete_create_with_metadata(self):
        #test for bug 1052479
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        expected_metadata = db.aggregate_metadata_get(ctxt, result['id'])
        self.assertThat(expected_metadata,
                        matchers.DictMatches(_get_fake_aggr_metadata()))
        db.aggregate_delete(ctxt, result['id'])
        result = _create_aggregate(metadata={'availability_zone':
            'fake_avail_zone'})
        expected_metadata = db.aggregate_metadata_get(ctxt, result['id'])
        self.assertEqual(expected_metadata, {'availability_zone':
            'fake_avail_zone'})

    def test_aggregate_create_low_privi_context(self):
        self.assertRaises(exception.AdminRequired,
                          db.aggregate_create,
                          self.context, _get_fake_aggr_values())

    def test_aggregate_get(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate_with_hosts(context=ctxt)
        expected = db.aggregate_get(ctxt, result['id'])
        self.assertEqual(_get_fake_aggr_hosts(), expected['hosts'])
        self.assertEqual(_get_fake_aggr_metadata(), expected['metadetails'])

    def test_aggregate_get_by_host(self):
        ctxt = context.get_admin_context()
        values = {'name': 'fake_aggregate2'}
        a1 = _create_aggregate_with_hosts(context=ctxt)
        a2 = _create_aggregate_with_hosts(context=ctxt, values=values)
        r1 = db.aggregate_get_by_host(ctxt, 'foo.openstack.org')
        self.assertEqual([a1['id'], a2['id']], [x['id'] for x in r1])

    def test_aggregate_get_by_host_with_key(self):
        ctxt = context.get_admin_context()
        values = {'name': 'fake_aggregate2'}
        a1 = _create_aggregate_with_hosts(context=ctxt,
                                          metadata={'goodkey': 'good'})
        a2 = _create_aggregate_with_hosts(context=ctxt, values=values)
        # filter result by key
        r1 = db.aggregate_get_by_host(ctxt, 'foo.openstack.org', key='goodkey')
        self.assertEqual([a1['id']], [x['id'] for x in r1])

    def test_aggregate_metadata_get_by_host(self):
        ctxt = context.get_admin_context()
        values = {'name': 'fake_aggregate2'}
        values2 = {'name': 'fake_aggregate3'}
        a1 = _create_aggregate_with_hosts(context=ctxt)
        a2 = _create_aggregate_with_hosts(context=ctxt, values=values)
        a3 = _create_aggregate_with_hosts(context=ctxt, values=values2,
                hosts=['bar.openstack.org'], metadata={'badkey': 'bad'})
        r1 = db.aggregate_metadata_get_by_host(ctxt, 'foo.openstack.org')
        self.assertEqual(r1['fake_key1'], set(['fake_value1']))
        self.assertFalse('badkey' in r1)

    def test_aggregate_metadata_get_by_host_with_key(self):
        ctxt = context.get_admin_context()
        values = {'name': 'fake_aggregate2'}
        values2 = {'name': 'fake_aggregate3'}
        a1 = _create_aggregate_with_hosts(context=ctxt)
        a2 = _create_aggregate_with_hosts(context=ctxt, values=values)
        a3 = _create_aggregate_with_hosts(context=ctxt, values=values2,
                hosts=['foo.openstack.org'], metadata={'good': 'value'})
        r1 = db.aggregate_metadata_get_by_host(ctxt, 'foo.openstack.org',
                                               key='good')
        self.assertEqual(r1['good'], set(['value']))
        self.assertFalse('fake_key1' in r1)
        # Delete metadata
        db.aggregate_metadata_delete(ctxt, a3['id'], 'good')
        r2 = db.aggregate_metadata_get_by_host(ctxt, 'foo.openstack.org',
                                               key='good')
        self.assertFalse('good' in r2)

    def test_aggregate_host_get_by_metadata_key(self):
        ctxt = context.get_admin_context()
        values = {'name': 'fake_aggregate2'}
        values2 = {'name': 'fake_aggregate3'}
        a1 = _create_aggregate_with_hosts(context=ctxt)
        a2 = _create_aggregate_with_hosts(context=ctxt, values=values)
        a3 = _create_aggregate_with_hosts(context=ctxt, values=values2,
                hosts=['foo.openstack.org'], metadata={'good': 'value'})
        r1 = db.aggregate_host_get_by_metadata_key(ctxt, key='good')
        self.assertEqual(r1, {'foo.openstack.org': set(['value'])})
        self.assertFalse('fake_key1' in r1)

    def test_aggregate_get_by_host_not_found(self):
        ctxt = context.get_admin_context()
        _create_aggregate_with_hosts(context=ctxt)
        self.assertEqual([], db.aggregate_get_by_host(ctxt, 'unknown_host'))

    def test_aggregate_delete_raise_not_found(self):
        ctxt = context.get_admin_context()
        # this does not exist!
        aggregate_id = 1
        self.assertRaises(exception.AggregateNotFound,
                          db.aggregate_delete,
                          ctxt, aggregate_id)

    def test_aggregate_delete(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata=None)
        db.aggregate_delete(ctxt, result['id'])
        expected = db.aggregate_get_all(ctxt)
        self.assertEqual(0, len(expected))
        aggregate = db.aggregate_get(ctxt.elevated(read_deleted='yes'),
                                     result['id'])
        self.assertEqual(aggregate['deleted'], True)

    def test_aggregate_update(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata={'availability_zone':
            'fake_avail_zone'})
        self.assertEqual(result['availability_zone'], 'fake_avail_zone')
        new_values = _get_fake_aggr_values()
        new_values['availability_zone'] = 'different_avail_zone'
        updated = db.aggregate_update(ctxt, 1, new_values)
        self.assertNotEqual(result['availability_zone'],
                            updated['availability_zone'])

    def test_aggregate_update_with_metadata(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata=None)
        values = _get_fake_aggr_values()
        values['metadata'] = _get_fake_aggr_metadata()
        values['availability_zone'] = 'different_avail_zone'
        db.aggregate_update(ctxt, 1, values)
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        updated = db.aggregate_get(ctxt, result['id'])
        self.assertThat(values['metadata'],
                        matchers.DictMatches(expected))
        self.assertNotEqual(result['availability_zone'],
                            updated['availability_zone'])

    def test_aggregate_update_with_existing_metadata(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        values = _get_fake_aggr_values()
        values['metadata'] = _get_fake_aggr_metadata()
        values['metadata']['fake_key1'] = 'foo'
        db.aggregate_update(ctxt, 1, values)
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        self.assertThat(values['metadata'], matchers.DictMatches(expected))

    def test_aggregate_update_raise_not_found(self):
        ctxt = context.get_admin_context()
        # this does not exist!
        aggregate_id = 1
        new_values = _get_fake_aggr_values()
        self.assertRaises(exception.AggregateNotFound,
                          db.aggregate_update, ctxt, aggregate_id, new_values)

    def test_aggregate_get_all(self):
        ctxt = context.get_admin_context()
        counter = 3
        for c in xrange(counter):
            _create_aggregate(context=ctxt,
                              values={'name': 'fake_aggregate_%d' % c},
                              metadata=None)
        results = db.aggregate_get_all(ctxt)
        self.assertEqual(len(results), counter)

    def test_aggregate_get_all_non_deleted(self):
        ctxt = context.get_admin_context()
        add_counter = 5
        remove_counter = 2
        aggregates = []
        for c in xrange(1, add_counter):
            values = {'name': 'fake_aggregate_%d' % c}
            aggregates.append(_create_aggregate(context=ctxt,
                                                values=values, metadata=None))
        for c in xrange(1, remove_counter):
            db.aggregate_delete(ctxt, aggregates[c - 1]['id'])
        results = db.aggregate_get_all(ctxt)
        self.assertEqual(len(results), add_counter - remove_counter)

    def test_aggregate_metadata_add(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata=None)
        metadata = _get_fake_aggr_metadata()
        db.aggregate_metadata_add(ctxt, result['id'], metadata)
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        self.assertThat(metadata, matchers.DictMatches(expected))

    def test_aggregate_metadata_update(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        metadata = _get_fake_aggr_metadata()
        key = metadata.keys()[0]
        db.aggregate_metadata_delete(ctxt, result['id'], key)
        new_metadata = {key: 'foo'}
        db.aggregate_metadata_add(ctxt, result['id'], new_metadata)
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        metadata[key] = 'foo'
        self.assertThat(metadata, matchers.DictMatches(expected))

    def test_aggregate_metadata_delete(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata=None)
        metadata = _get_fake_aggr_metadata()
        db.aggregate_metadata_add(ctxt, result['id'], metadata)
        db.aggregate_metadata_delete(ctxt, result['id'], metadata.keys()[0])
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        del metadata[metadata.keys()[0]]
        self.assertThat(metadata, matchers.DictMatches(expected))

    def test_aggregate_remove_availability_zone(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt, metadata={'availability_zone':
            'fake_avail_zone'})
        db.aggregate_metadata_delete(ctxt, result['id'], 'availability_zone')
        expected = db.aggregate_metadata_get(ctxt, result['id'])
        aggregate = db.aggregate_get(ctxt, result['id'])
        self.assertEquals(aggregate['availability_zone'], None)
        self.assertThat({}, matchers.DictMatches(expected))

    def test_aggregate_metadata_delete_raise_not_found(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        self.assertRaises(exception.AggregateMetadataNotFound,
                          db.aggregate_metadata_delete,
                          ctxt, result['id'], 'foo_key')

    def test_aggregate_host_add(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate_with_hosts(context=ctxt, metadata=None)
        expected = db.aggregate_host_get_all(ctxt, result['id'])
        self.assertEqual(_get_fake_aggr_hosts(), expected)

    def test_aggregate_host_re_add(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate_with_hosts(context=ctxt, metadata=None)
        host = _get_fake_aggr_hosts()[0]
        db.aggregate_host_delete(ctxt, result['id'], host)
        db.aggregate_host_add(ctxt, result['id'], host)
        expected = db.aggregate_host_get_all(ctxt, result['id'])
        self.assertEqual(len(expected), 1)

    def test_aggregate_host_add_duplicate_works(self):
        ctxt = context.get_admin_context()
        r1 = _create_aggregate_with_hosts(context=ctxt, metadata=None)
        r2 = _create_aggregate_with_hosts(ctxt,
                          values={'name': 'fake_aggregate2'},
                          metadata={'availability_zone': 'fake_avail_zone2'})
        h1 = db.aggregate_host_get_all(ctxt, r1['id'])
        h2 = db.aggregate_host_get_all(ctxt, r2['id'])
        self.assertEqual(h1, h2)

    def test_aggregate_host_add_duplicate_raise_exist_exc(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate_with_hosts(context=ctxt, metadata=None)
        self.assertRaises(exception.AggregateHostExists,
                          db.aggregate_host_add,
                          ctxt, result['id'], _get_fake_aggr_hosts()[0])

    def test_aggregate_host_add_raise_not_found(self):
        ctxt = context.get_admin_context()
        # this does not exist!
        aggregate_id = 1
        host = _get_fake_aggr_hosts()[0]
        self.assertRaises(exception.AggregateNotFound,
                          db.aggregate_host_add,
                          ctxt, aggregate_id, host)

    def test_aggregate_host_delete(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate_with_hosts(context=ctxt, metadata=None)
        db.aggregate_host_delete(ctxt, result['id'],
                                 _get_fake_aggr_hosts()[0])
        expected = db.aggregate_host_get_all(ctxt, result['id'])
        self.assertEqual(0, len(expected))

    def test_aggregate_host_delete_raise_not_found(self):
        ctxt = context.get_admin_context()
        result = _create_aggregate(context=ctxt)
        self.assertRaises(exception.AggregateHostNotFound,
                          db.aggregate_host_delete,
                          ctxt, result['id'], _get_fake_aggr_hosts()[0])


class CapacityTestCase(test.TestCase):
    def setUp(self):
        super(CapacityTestCase, self).setUp()

        self.ctxt = context.get_admin_context()

        service_dict = dict(host='host1', binary='binary1',
                            topic='compute', report_count=1,
                            disabled=False)
        self.service = db.service_create(self.ctxt, service_dict)

        self.compute_node_dict = dict(vcpus=2, memory_mb=1024, local_gb=2048,
                                 vcpus_used=0, memory_mb_used=0,
                                 local_gb_used=0, free_ram_mb=1024,
                                 free_disk_gb=2048, hypervisor_type="xen",
                                 hypervisor_version=1, cpu_info="",
                                 running_vms=0, current_workload=0,
                                 service_id=self.service['id'])
        # add some random stats
        stats = dict(num_instances=3, num_proj_12345=2,
                     num_proj_23456=2, num_vm_building=3)
        self.compute_node_dict['stats'] = stats

        self.flags(reserved_host_memory_mb=0)
        self.flags(reserved_host_disk_mb=0)

    def _create_helper(self, host):
        self.compute_node_dict['host'] = host
        return db.compute_node_create(self.ctxt, self.compute_node_dict)

    def _stats_as_dict(self, stats):
        d = {}
        for s in stats:
            key = s['key']
            d[key] = s['value']
        return d

    def test_compute_node_create(self):
        item = self._create_helper('host1')
        self.assertEquals(item['free_ram_mb'], 1024)
        self.assertEquals(item['free_disk_gb'], 2048)
        self.assertEquals(item['running_vms'], 0)
        self.assertEquals(item['current_workload'], 0)

        stats = self._stats_as_dict(item['stats'])
        self.assertEqual(3, stats['num_instances'])
        self.assertEqual(2, stats['num_proj_12345'])
        self.assertEqual(3, stats['num_vm_building'])

    def test_compute_node_get_all(self):
        item = self._create_helper('host1')
        nodes = db.compute_node_get_all(self.ctxt)
        self.assertEqual(1, len(nodes))

        node = nodes[0]
        self.assertEqual(2, node['vcpus'])

        stats = self._stats_as_dict(node['stats'])
        self.assertEqual(3, int(stats['num_instances']))
        self.assertEqual(2, int(stats['num_proj_12345']))
        self.assertEqual(3, int(stats['num_vm_building']))

    def test_compute_node_update(self):
        item = self._create_helper('host1')

        compute_node_id = item['id']
        stats = self._stats_as_dict(item['stats'])

        # change some values:
        stats['num_instances'] = 8
        stats['num_tribbles'] = 1
        values = {
            'vcpus': 4,
            'stats': stats,
        }
        item = db.compute_node_update(self.ctxt, compute_node_id, values)
        stats = self._stats_as_dict(item['stats'])

        self.assertEqual(4, item['vcpus'])
        self.assertEqual(8, int(stats['num_instances']))
        self.assertEqual(2, int(stats['num_proj_12345']))
        self.assertEqual(1, int(stats['num_tribbles']))

    def test_compute_node_stat_prune(self):
        item = self._create_helper('host1')
        for stat in item['stats']:
            if stat['key'] == 'num_instances':
                num_instance_stat = stat
                break

        values = {
            'stats': dict(num_instances=1)
        }
        db.compute_node_update(self.ctxt, item['id'], values, prune_stats=True)
        item = db.compute_node_get_all(self.ctxt)[0]
        self.assertEqual(1, len(item['stats']))

        stat = item['stats'][0]
        self.assertEqual(num_instance_stat['id'], stat['id'])
        self.assertEqual(num_instance_stat['key'], stat['key'])
        self.assertEqual(1, int(stat['value']))


class MigrationTestCase(test.TestCase):

    def setUp(self):
        super(MigrationTestCase, self).setUp()
        self.ctxt = context.get_admin_context()

        self._create()
        self._create()
        self._create(status='reverted')
        self._create(status='confirmed')
        self._create(source_compute='host2', source_node='b',
                dest_compute='host1', dest_node='a')
        self._create(source_compute='host2', dest_compute='host3')
        self._create(source_compute='host3', dest_compute='host4')

    def _create(self, status='migrating', source_compute='host1',
                source_node='a', dest_compute='host2', dest_node='b'):

        values = {'host': source_compute}
        instance = db.instance_create(self.ctxt, values)

        values = {'status': status, 'source_compute': source_compute,
                  'source_node': source_node, 'dest_compute': dest_compute,
                  'dest_node': dest_node, 'instance_uuid': instance['uuid']}
        db.migration_create(self.ctxt, values)

    def _assert_in_progress(self, migrations):
        for migration in migrations:
            self.assertNotEqual('confirmed', migration['status'])
            self.assertNotEqual('reverted', migration['status'])

    def test_in_progress_host1_nodea(self):
        migrations = db.migration_get_in_progress_by_host_and_node(self.ctxt,
                'host1', 'a')
        # 2 as source + 1 as dest
        self.assertEqual(3, len(migrations))
        self._assert_in_progress(migrations)

    def test_in_progress_host1_nodeb(self):
        migrations = db.migration_get_in_progress_by_host_and_node(self.ctxt,
                'host1', 'b')
        # some migrations are to/from host1, but none with a node 'b'
        self.assertEqual(0, len(migrations))

    def test_in_progress_host2_nodeb(self):
        migrations = db.migration_get_in_progress_by_host_and_node(self.ctxt,
                'host2', 'b')
        # 2 as dest, 1 as source
        self.assertEqual(3, len(migrations))
        self._assert_in_progress(migrations)

    def test_instance_join(self):
        migrations = db.migration_get_in_progress_by_host_and_node(self.ctxt,
                'host2', 'b')
        for migration in migrations:
            instance = migration['instance']
            self.assertEqual(migration['instance_uuid'], instance['uuid'])


class TestFixedIPGetByNetworkHost(test.TestCase):
    def test_not_found_exception(self):
        ctxt = context.get_admin_context()

        self.assertRaises(
            exception.FixedIpNotFoundForNetworkHost,
            db.fixed_ip_get_by_network_host,
            ctxt, 1, 'ignore')

    def test_fixed_ip_found(self):
        ctxt = context.get_admin_context()
        db.fixed_ip_create(ctxt, dict(network_id=1, host='host'))

        fip = db.fixed_ip_get_by_network_host(ctxt, 1, 'host')

        self.assertEquals(1, fip['network_id'])
        self.assertEquals('host', fip['host'])


class TestIpAllocation(test.TestCase):

    def setUp(self):
        super(TestIpAllocation, self).setUp()
        self.ctxt = context.get_admin_context()
        self.instance = db.instance_create(self.ctxt, {})
        self.network = db.network_create_safe(self.ctxt, {})

    def create_fixed_ip(self, **params):
        default_params = {'address': '192.168.0.1'}
        default_params.update(params)
        return db.fixed_ip_create(self.ctxt, default_params)

    def test_fixed_ip_associate_fails_if_ip_not_in_network(self):
        self.assertRaises(exception.FixedIpNotFoundForNetwork,
                          db.fixed_ip_associate,
                          self.ctxt, None, self.instance['uuid'])

    def test_fixed_ip_associate_fails_if_ip_in_use(self):
        address = self.create_fixed_ip(instance_uuid=self.instance['uuid'])
        self.assertRaises(exception.FixedIpAlreadyInUse,
                          db.fixed_ip_associate,
                          self.ctxt, address, self.instance['uuid'])

    def test_fixed_ip_associate_succeeds(self):
        address = self.create_fixed_ip(network_id=self.network['id'])
        db.fixed_ip_associate(self.ctxt, address, self.instance['uuid'],
                              network_id=self.network['id'])
        fixed_ip = db.fixed_ip_get_by_address(self.ctxt, address)
        self.assertEqual(fixed_ip['instance_uuid'], self.instance['uuid'])

    def test_fixed_ip_associate_succeeds_and_sets_network(self):
        address = self.create_fixed_ip()
        db.fixed_ip_associate(self.ctxt, address, self.instance['uuid'],
                              network_id=self.network['id'])
        fixed_ip = db.fixed_ip_get_by_address(self.ctxt, address)
        self.assertEqual(fixed_ip['instance_uuid'], self.instance['uuid'])
        self.assertEqual(fixed_ip['network_id'], self.network['id'])


class InstanceDestroyConstraints(test.TestCase):

    def test_destroy_with_equal_any_constraint_met(self):
        ctx = context.get_admin_context()
        instance = db.instance_create(ctx, {'task_state': 'deleting'})
        constraint = db.constraint(task_state=db.equal_any('deleting'))
        db.instance_destroy(ctx, instance['uuid'], constraint)
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          ctx, instance['uuid'])

    def test_destroy_with_equal_any_constraint_not_met(self):
        ctx = context.get_admin_context()
        instance = db.instance_create(ctx, {'vm_state': 'resize'})
        constraint = db.constraint(vm_state=db.equal_any('active', 'error'))
        self.assertRaises(exception.ConstraintNotMet, db.instance_destroy,
                          ctx, instance['uuid'], constraint)
        instance = db.instance_get_by_uuid(ctx, instance['uuid'])
        self.assertFalse(instance['deleted'])

    def test_destroy_with_not_equal_constraint_met(self):
        ctx = context.get_admin_context()
        instance = db.instance_create(ctx, {'task_state': 'deleting'})
        constraint = db.constraint(task_state=db.not_equal('error', 'resize'))
        db.instance_destroy(ctx, instance['uuid'], constraint)
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          ctx, instance['uuid'])

    def test_destroy_with_not_equal_constraint_not_met(self):
        ctx = context.get_admin_context()
        instance = db.instance_create(ctx, {'vm_state': 'active'})
        constraint = db.constraint(vm_state=db.not_equal('active', 'error'))
        self.assertRaises(exception.ConstraintNotMet, db.instance_destroy,
                          ctx, instance['uuid'], constraint)
        instance = db.instance_get_by_uuid(ctx, instance['uuid'])
        self.assertFalse(instance['deleted'])


class VolumeUsageDBApiTestCase(test.TestCase):
    def setUp(self):
        super(VolumeUsageDBApiTestCase, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

    def test_vol_usage_update_no_totals_update(self):
        ctxt = context.get_admin_context()
        now = timeutils.utcnow()
        timeutils.set_time_override(now)
        start_time = now - datetime.timedelta(seconds=10)
        refreshed_time = now - datetime.timedelta(seconds=5)

        expected_vol_usages = [{'volume_id': u'1',
                                'curr_reads': 1000,
                                'curr_read_bytes': 2000,
                                'curr_writes': 3000,
                                'curr_write_bytes': 4000},
                               {'volume_id': u'2',
                                'curr_reads': 100,
                                'curr_read_bytes': 200,
                                'curr_writes': 300,
                                'curr_write_bytes': 400}]

        def _compare(vol_usage, expected):
            for key, value in expected.items():
                self.assertEqual(vol_usage[key], value)

        vol_usages = db.vol_get_usage_by_time(ctxt, start_time)
        self.assertEqual(len(vol_usages), 0)

        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=10, rd_bytes=20,
                                        wr_req=30, wr_bytes=40, instance_id=1)
        vol_usage = db.vol_usage_update(ctxt, 2, rd_req=100, rd_bytes=200,
                                        wr_req=300, wr_bytes=400,
                                        instance_id=1)
        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=1000, rd_bytes=2000,
                                        wr_req=3000, wr_bytes=4000,
                                        instance_id=1,
                                        last_refreshed=refreshed_time)

        vol_usages = db.vol_get_usage_by_time(ctxt, start_time)
        self.assertEqual(len(vol_usages), 2)
        _compare(vol_usages[0], expected_vol_usages[0])
        _compare(vol_usages[1], expected_vol_usages[1])
        timeutils.clear_time_override()

    def test_vol_usage_update_totals_update(self):
        ctxt = context.get_admin_context()
        now = timeutils.utcnow()
        timeutils.set_time_override(now)
        start_time = now - datetime.timedelta(seconds=10)
        expected_vol_usages = {'volume_id': u'1',
                               'tot_reads': 600,
                               'tot_read_bytes': 800,
                               'tot_writes': 1000,
                               'tot_write_bytes': 1200,
                               'curr_reads': 0,
                               'curr_read_bytes': 0,
                               'curr_writes': 0,
                               'curr_write_bytes': 0}

        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=100, rd_bytes=200,
                                        wr_req=300, wr_bytes=400,
                                        instance_id=1)
        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=200, rd_bytes=300,
                                        wr_req=400, wr_bytes=500,
                                        instance_id=1,
                                        update_totals=True)
        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=300, rd_bytes=400,
                                        wr_req=500, wr_bytes=600,
                                        instance_id=1)
        vol_usage = db.vol_usage_update(ctxt, 1, rd_req=400, rd_bytes=500,
                                        wr_req=600, wr_bytes=700,
                                        instance_id=1,
                                        update_totals=True)

        vol_usages = db.vol_get_usage_by_time(ctxt, start_time)

        self.assertEquals(1, len(vol_usages))
        for key, value in expected_vol_usages.items():
            self.assertEqual(vol_usages[0][key], value)
        timeutils.clear_time_override()


class TaskLogTestCase(test.TestCase):

    def setUp(self):
        super(TaskLogTestCase, self).setUp()
        self.context = context.get_admin_context()
        now = timeutils.utcnow()
        self.begin = now - datetime.timedelta(seconds=10)
        self.end = now - datetime.timedelta(seconds=5)
        self.task_name = 'fake-task-name'
        self.host = 'fake-host'
        self.message = 'Fake task message'
        db.task_log_begin_task(self.context, self.task_name, self.begin,
                               self.end, self.host, message=self.message)

    def test_task_log_get(self):
        result = db.task_log_get(self.context, self.task_name, self.begin,
                                 self.end, self.host)
        self.assertEqual(result['task_name'], self.task_name)
        self.assertEqual(result['period_beginning'], self.begin)
        self.assertEqual(result['period_ending'], self.end)
        self.assertEqual(result['host'], self.host)
        self.assertEqual(result['message'], self.message)

    def test_task_log_get_all(self):
        result = db.task_log_get_all(self.context, self.task_name, self.begin,
                                     self.end, host=self.host)
        self.assertEqual(len(result), 1)

    def test_task_log_begin_task(self):
        db.task_log_begin_task(self.context, 'fake', self.begin,
                               self.end, self.host, message=self.message)
        result = db.task_log_get(self.context, 'fake', self.begin,
                                 self.end, self.host)
        self.assertEqual(result['task_name'], 'fake')

    def test_task_log_begin_task_duplicate(self):
        params = (self.context, 'fake', self.begin, self.end, self.host)
        db.task_log_begin_task(*params, message=self.message)
        self.assertRaises(exception.TaskAlreadyRunning,
                          db.task_log_begin_task,
                          *params, message=self.message)

    def test_task_log_end_task(self):
        errors = 1
        db.task_log_end_task(self.context, self.task_name, self.begin,
                            self.end, self.host, errors, message=self.message)
        result = db.task_log_get(self.context, self.task_name, self.begin,
                                 self.end, self.host)
        self.assertEqual(result['errors'], 1)


class ArchiveTestCase(test.TestCase):

    def setUp(self):
        super(ArchiveTestCase, self).setUp()
        self.context = context.get_admin_context()
        engine = get_engine()
        self.conn = engine.connect()
        self.metadata = MetaData()
        self.metadata.bind = engine
        self.table1 = Table("instance_id_mappings",
                           self.metadata,
                           autoload=True)
        self.shadow_table1 = Table("shadow_instance_id_mappings",
                                  self.metadata,
                                  autoload=True)
        self.table2 = Table("dns_domains",
                           self.metadata,
                           autoload=True)
        self.shadow_table2 = Table("shadow_dns_domains",
                                  self.metadata,
                                  autoload=True)
        self.uuidstrs = []
        for unused in xrange(6):
            self.uuidstrs.append(stdlib_uuid.uuid4().hex)

    def tearDown(self):
        super(ArchiveTestCase, self).tearDown()
        delete_statement1 = self.table1.delete(
                                self.table1.c.uuid.in_(self.uuidstrs))
        self.conn.execute(delete_statement1)
        delete_statement2 = self.shadow_table1.delete(
                                self.shadow_table1.c.uuid.in_(self.uuidstrs))
        self.conn.execute(delete_statement2)
        delete_statement3 = self.table2.delete(self.table2.c.domain.in_(
                                               self.uuidstrs))
        self.conn.execute(delete_statement3)
        delete_statement4 = self.shadow_table2.delete(
                                self.shadow_table2.c.domain.in_(self.uuidstrs))
        self.conn.execute(delete_statement4)

    def test_archive_deleted_rows(self):
        # Add 6 rows to table
        for uuidstr in self.uuidstrs:
            insert_statement = self.table1.insert().values(uuid=uuidstr)
            self.conn.execute(insert_statement)
        # Set 4 to deleted
        update_statement = self.table1.update().\
                where(self.table1.c.uuid.in_(self.uuidstrs[:4]))\
                .values(deleted=True)
        self.conn.execute(update_statement)
        query1 = select([self.table1]).where(self.table1.c.uuid.in_(
                                             self.uuidstrs))
        rows1 = self.conn.execute(query1).fetchall()
        # Verify we have 6 in main
        self.assertEqual(len(rows1), 6)
        query2 = select([self.shadow_table1]).\
                where(self.shadow_table1.c.uuid.in_(self.uuidstrs))
        rows2 = self.conn.execute(query2).fetchall()
        # Verify we have 0 in shadow
        self.assertEqual(len(rows2), 0)
        # Archive 2 rows
        db.archive_deleted_rows(self.context, max_rows=2)
        rows3 = self.conn.execute(query1).fetchall()
        # Verify we have 4 left in main
        self.assertEqual(len(rows3), 4)
        rows4 = self.conn.execute(query2).fetchall()
        # Verify we have 2 in shadow
        self.assertEqual(len(rows4), 2)
        # Archive 2 more rows
        db.archive_deleted_rows(self.context, max_rows=2)
        rows5 = self.conn.execute(query1).fetchall()
        # Verify we have 2 left in main
        self.assertEqual(len(rows5), 2)
        rows6 = self.conn.execute(query2).fetchall()
        # Verify we have 4 in shadow
        self.assertEqual(len(rows6), 4)
        # Try to archive more, but there are no deleted rows left.
        db.archive_deleted_rows(self.context, max_rows=2)
        rows7 = self.conn.execute(query1).fetchall()
        # Verify we still have 2 left in main
        self.assertEqual(len(rows7), 2)
        rows8 = self.conn.execute(query2).fetchall()
        # Verify we still have 4 in shadow
        self.assertEqual(len(rows8), 4)

    def test_archive_deleted_rows_for_table(self):
        tablename = "instance_id_mappings"
        # Add 6 rows to table
        for uuidstr in self.uuidstrs:
            insert_statement = self.table1.insert().values(uuid=uuidstr)
            self.conn.execute(insert_statement)
        # Set 4 to deleted
        update_statement = self.table1.update().\
                where(self.table1.c.uuid.in_(self.uuidstrs[:4]))\
                .values(deleted=True)
        self.conn.execute(update_statement)
        query1 = select([self.table1]).where(self.table1.c.uuid.in_(
                                             self.uuidstrs))
        rows1 = self.conn.execute(query1).fetchall()
        # Verify we have 6 in main
        self.assertEqual(len(rows1), 6)
        query2 = select([self.shadow_table1]).\
                where(self.shadow_table1.c.uuid.in_(self.uuidstrs))
        rows2 = self.conn.execute(query2).fetchall()
        # Verify we have 0 in shadow
        self.assertEqual(len(rows2), 0)
        # Archive 2 rows
        db.archive_deleted_rows_for_table(self.context, tablename, max_rows=2)
        rows3 = self.conn.execute(query1).fetchall()
        # Verify we have 4 left in main
        self.assertEqual(len(rows3), 4)
        rows4 = self.conn.execute(query2).fetchall()
        # Verify we have 2 in shadow
        self.assertEqual(len(rows4), 2)
        # Archive 2 more rows
        db.archive_deleted_rows_for_table(self.context, tablename, max_rows=2)
        rows5 = self.conn.execute(query1).fetchall()
        # Verify we have 2 left in main
        self.assertEqual(len(rows5), 2)
        rows6 = self.conn.execute(query2).fetchall()
        # Verify we have 4 in shadow
        self.assertEqual(len(rows6), 4)
        # Try to archive more, but there are no deleted rows left.
        db.archive_deleted_rows_for_table(self.context, tablename, max_rows=2)
        rows7 = self.conn.execute(query1).fetchall()
        # Verify we still have 2 left in main
        self.assertEqual(len(rows7), 2)
        rows8 = self.conn.execute(query2).fetchall()
        # Verify we still have 4 in shadow
        self.assertEqual(len(rows8), 4)

    def test_archive_deleted_rows_no_id_column(self):
        uuidstr0 = self.uuidstrs[0]
        insert_statement = self.table2.insert().values(domain=uuidstr0)
        self.conn.execute(insert_statement)
        update_statement = self.table2.update().\
                           where(self.table2.c.domain == uuidstr0).\
                           values(deleted=True)
        self.conn.execute(update_statement)
        query1 = select([self.table2], self.table2.c.domain == uuidstr0)
        rows1 = self.conn.execute(query1).fetchall()
        self.assertEqual(len(rows1), 1)
        query2 = select([self.shadow_table2],
                        self.shadow_table2.c.domain == uuidstr0)
        rows2 = self.conn.execute(query2).fetchall()
        self.assertEqual(len(rows2), 0)
        db.archive_deleted_rows(self.context, max_rows=1)
        rows3 = self.conn.execute(query1).fetchall()
        self.assertEqual(len(rows3), 0)
        rows4 = self.conn.execute(query2).fetchall()
        self.assertEqual(len(rows4), 1)
