# Copyright (c) LinkedIn Corporation. All rights reserved. Licensed under the BSD-2 Clause license.
# See LICENSE in the project root for license information.

from __future__ import absolute_import

from gevent import socket
import msgpack
import time
import hmac
import hashlib
import base64
import re
import datetime
import logging
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from urlparse import parse_qs
import ujson
from falcon import HTTP_200, HTTP_201, HTTP_204, HTTPBadRequest, HTTPNotFound, HTTPUnauthorized, HTTPForbidden, API
from sqlalchemy.exc import IntegrityError
from importlib import import_module
import yaml

from streql import equals

from . import db
from . import utils
from . import cache
from iris_api.sender import auditlog


from .constants import (
    XFRAME, XCONTENTTYPEOPTIONS, XXSSPROTECTION
)

from .plugins import init_plugins, find_plugin
from .validators import init_validators, run_validation, IrisValidationException

logger = logging.getLogger(__name__)

operators = {
    '': '%s = %s',
    'eq': '%s = %s',
    'in': '%s in %s',
    'ne': '%s != %s',
    'gt': '%s > %s',
    'ge': '%s >= %s',
    'lt': '%s < %s',
    'le': '%s <= %s',
    'contains': '%s LIKE CONCAT("%%%%", %s, "%%%%")',
    'startswith': '%s LIKE CONCAT(%s, "%%%%")',
    'endswith': '%s LIKE CONCAT("%%%%", %s)',
}

message_columns = {
    'id': '`message`.`id` as `id`',
    'active': '`message`.`active` as `active`',
    'batch': '`message`.`batch` as `batch`',
    'created': 'UNIX_TIMESTAMP(`message`.`created`) as `created`',
    'sent': 'UNIX_TIMESTAMP(`message`.`sent`) as `sent`',
    'destination': '`message`.`destination` as `destination`',
    'subject': '`message`.`subject` as `subject`',
    'incident_id': '`message`.`incident_id` as `incident_id`',
    'mode': '`mode`.`name` as `mode`',
    'application': '`application`.`name` as `application`',
    'priority': '`priority`.`name` as `priority`',
    'target': '`target`.`name` as `target`',
    'body': '`message`.`body` as `body`',
    'mode_changed': 'exists(SELECT 1 FROM `message_changelog` WHERE `message_id` = `message`.`id` AND `change_type` = %(mode_change)s) as mode_changed',
    'target_changed': 'exists(SELECT 1 FROM `message_changelog` WHERE `message_id` = `message`.`id` AND `change_type` = %(target_change)s) as target_changed'
}

message_filters = {
    'id': '`message`.`id`',
    'active': '`message`.`active`',
    'batch': '`message`.`batch`',
    'created': 'UNIX_TIMESTAMP(`message`.`created`)',
    'sent': 'UNIX_TIMESTAMP(`message`.`sent`)',
    'destination': '`message`.`destination`',
    'subject': '`message`.`subject`',
    'incident_id': '`message`.`incident_id`',
    'mode': '`mode`.`name`',
    'application': '`application`.`name`',
    'priority': '`priority`.`name`',
    'target': '`target`.`name`',
    'body': '`message`.`body`',
}

message_filter_types = {
    'id': int,
    'created': int,
    'sent': int,
}

message_query = '''SELECT %s FROM `message`
 JOIN `priority` ON `message`.`priority_id` = `priority`.`id`
 JOIN `application` ON `message`.`application_id` = `application`.`id`
 JOIN `mode` ON `message`.`mode_id` = `mode`.`id`
 JOIN `target` ON `message`.`target_id`=`target`.`id`'''

single_message_query = '''SELECT `message`.`id` as `id`,
    `message`.`active` as `active`,
    `message`.`batch` as `batch`,
    `message`.`body` as `body`,
    UNIX_TIMESTAMP(`message`.`created`) as `created`,
    UNIX_TIMESTAMP(`message`.`sent`) as `sent`,
    `message`.`destination` as `destination`,
    `message`.`subject` as `subject`,
    `message`.`incident_id` as `incident_id`,
    `mode`.`name` as `mode`,
    `application`.`name` as `application`,
    `priority`.`name` as `priority`,
    `target`.`name` as `target`
FROM `message`
JOIN `priority` ON `message`.`priority_id` = `priority`.`id`
JOIN `application` ON `message`.`application_id` = `application`.`id`
JOIN `mode` ON `message`.`mode_id` = `mode`.`id`
JOIN `target` ON `message`.`target_id`=`target`.`id`
WHERE `message`.`id` = %s'''

message_audit_log_query = '''SELECT `id`, `date`, `old`, `new`, `change_type`, `description`
                             FROM `message_changelog`
                             WHERE `message_id` = %s
                             ORDER BY `date` DESC'''

incident_columns = {
    'id': '`incident`.`id` as `id`',
    'plan': '`plan`.`name` as `plan`',
    'plan_id': '`incident`.`plan_id` as `plan_id`',
    'active': '`incident`.`active` as `active`',
    'updated': 'UNIX_TIMESTAMP(`incident`.`updated`) as `updated`',
    'application': '`application`.`name` as `application`',
    'context': '`incident`.`context` as `context`',
    'created': 'UNIX_TIMESTAMP(`incident`.`created`) as `created`',
    'owner': '`target`.`name` as `owner`',
    'current_step': '`incident`.`current_step` as `current_step`',
}

incident_filters = {
    'id': '`incident`.`id`',
    'plan': '`plan`.`name`',
    'plan_id': '`incident`.`plan_id`',
    'active': '`incident`.`active`',
    'updated': 'UNIX_TIMESTAMP(`incident`.`updated`)',
    'application': '`application`.`name`',
    'context': '`incident`.`context`',
    'created': 'UNIX_TIMESTAMP(`incident`.`created`)',
    'owner': '`target`.`name`',
    'current_step': '`incident`.`current_step`',
}

incident_filter_types = {
    'id': int,
    'plan_id': int,
    'updated': int,
    'created': int,
    'current_step': int,
}

incident_query = '''SELECT %s FROM `incident`
 JOIN `plan` ON `incident`.`plan_id` = `plan`.`id`
 LEFT OUTER JOIN `target` ON `incident`.`owner_id` = `target`.`id`
 JOIN `application` ON `incident`.`application_id` = `application`.`id`'''

single_incident_query = '''SELECT `incident`.`id` as `id`,
    `incident`.`plan_id` as `plan_id`,
    `plan`.`name` as `plan`,
    UNIX_TIMESTAMP(`incident`.`created`) as `created`,
    UNIX_TIMESTAMP(`incident`.`updated`) as `updated`,
    `incident`.`context` as `context`,
    `target`.`name` as `owner`,
    `application`.`name` as `application`,
    `incident`.`current_step` as `current_step`,
    `incident`.`active` as `active`
FROM `incident`
JOIN `plan` ON `incident`.`plan_id` = `plan`.`id`
LEFT OUTER JOIN `target` ON `incident`.`owner_id` = `target`.`id`
JOIN `application` ON `incident`.`application_id` = `application`.`id`
WHERE `incident`.`id` = %s'''

single_incident_query_steps = '''SELECT `message`.`id` as `id`,
    `target`.`name` as `name`,
    `mode`.`name` as `mode`,
    `priority`.`name` as `priority`,
    UNIX_TIMESTAMP(`message`.`created`) as `created`,
    UNIX_TIMESTAMP(`message`.`sent`) as `sent`,
    `plan_notification`.`step` as `step`,
    exists(SELECT 1 FROM `message_changelog` WHERE `message_id` = `message`.`id` AND `change_type` = %s) as mode_changed,
    exists(SELECT 1 FROM `message_changelog` WHERE `message_id` = `message`.`id` AND `change_type` = %s) as target_changed
FROM `message`
JOIN `priority` ON `message`.`priority_id` = `priority`.`id`
JOIN `mode` ON `message`.`mode_id` = `mode`.`id`
JOIN `target` ON `message`.`target_id` = `target`.`id`
JOIN `plan_notification` ON `message`.`plan_notification_id` = `plan_notification`.`id`
WHERE `message`.`incident_id` = %s
ORDER BY `message`.`sent`'''

plan_columns = {
    'id': '`plan`.`id` as `id`',
    'name': '`plan`.`name` as `name`',
    'threshold_window': '`plan`.`threshold_window` as `threshold_window`',
    'threshold_count': '`plan`.`threshold_count` as `threshold_count`',
    'aggregation_window': '`plan`.`aggregation_window` as `aggregation_window`',
    'aggregation_reset': '`plan`.`aggregation_reset` as `aggregation_reset`',
    'tracking_type': '`plan`.`tracking_type` as `tracking_type`',
    'tracking_key': '`plan`.`tracking_key` as `tracking_key`',
    'tracking_template': '`plan`.`tracking_template` as `tracking_template`',
    'description': '`plan`.`description` as `description`',
    'created': 'UNIX_TIMESTAMP(`plan`.`created`) as `created`',
    'creator': '`target`.`name` as `creator`',
    'active': 'IF(`plan_active`.`plan_id` IS NULL, FALSE, TRUE) as `active`',
}

plan_filters = {
    'id': '`plan`.`id`',
    'name': '`plan`.`name`',
    'threshold_window': '`plan`.`threshold_window`',
    'threshold_count': '`plan`.`threshold_count`',
    'aggregation_window': '`plan`.`aggregation_window`',
    'aggregation_reset': '`plan`.`aggregation_reset`',
    'description': '`plan`.`description`',
    'created': 'UNIX_TIMESTAMP(`plan`.`created`)',
    'creator': '`target`.`name`',
    'active': '`plan_active`.`plan_id`',
}

plan_filter_types = {
    'id': int,
    'created': int,
    'threshold_count': int,
    'threshold_window': int,
    'aggregation_window': int,
    'aggregation_reset': int,
}

plan_query = '''SELECT %s FROM `plan` JOIN `target` ON `plan`.`user_id` = `target`.`id`
LEFT OUTER JOIN `plan_active` ON `plan`.`id` = `plan_active`.`plan_id`'''

single_plan_query = '''SELECT `plan`.`id` as `id`, `plan`.`name` as `name`,
    `plan`.`threshold_window` as `threshold_window`, `plan`.`threshold_count` as `threshold_count`,
    `plan`.`aggregation_window` as `aggregation_window`, `plan`.`aggregation_reset` as `aggregation_reset`,
    `plan`.`description` as `description`, UNIX_TIMESTAMP(`plan`.`created`) as `created`,
    `target`.`name` as `creator`, IF(`plan_active`.`plan_id` IS NULL, FALSE, TRUE) as `active`,
    `plan`.`tracking_type` as `tracking_type`, `plan`.`tracking_key` as `tracking_key`,
    `plan`.`tracking_template` as `tracking_template`
FROM `plan` JOIN `target` ON `plan`.`user_id` = `target`.`id`
LEFT OUTER JOIN `plan_active` ON `plan`.`id` = `plan_active`.`plan_id`'''

single_plan_query_steps = '''SELECT `plan_notification`.`id` as `id`,
    `plan_notification`.`step` as `step`,
    `plan_notification`.`repeat` as `repeat`,
    `plan_notification`.`wait` as `wait`,
    `plan_notification`.`template_id` as `template_id`,
    `target_role`.`name` as `role`,
    `target`.`name` as `target`,
    `plan_notification`.`template` as `template`,
    `priority`.`name` as `priority`
FROM `plan_notification`
JOIN `target` ON `plan_notification`.`target_id` = `target`.`id`
JOIN `target_role` ON `plan_notification`.`role_id` = `target_role`.`id`
JOIN `priority` ON `plan_notification`.`priority_id` = `priority`.`id`
WHERE `plan_notification`.`plan_id` = %s
ORDER BY `plan_notification`.`step`'''

template_columns = {
    'id': '`template`.`id` as `id`',
    'name': '`template`.`name` as `name`',
    'creator': '`target`.`name` as `creator`',
    'created': 'UNIX_TIMESTAMP(`template`.`created`) as `created`',
    'active': 'IF(`template_active`.`template_id` IS NULL, FALSE, TRUE) as `active`',
}

template_filters = {
    'id': '`template`.`id`',
    'name': '`template`.`name`',
    'creator': '`target`.`name`',
    'created': 'UNIX_TIMESTAMP(`template`.`created`)',
    'active': '`template_active`.`template_id`',
}

template_filter_types = {
    'id': int,
    'created': int,
}

template_query = '''SELECT %s FROM `template`
JOIN `target` ON `template`.`user_id`=`target`.`id`
LEFT OUTER JOIN `template_active` ON `template`.`id` = `template_active`.`template_id`'''

single_template_query = '''SELECT
    `template`.`id` as `id`,
    `template`.`name` as `name`,
    IF(`template_active`.`template_id` IS NULL, FALSE, TRUE) as `active`,
    `target`.`name` as `creator`,
    UNIX_TIMESTAMP(`template`.`created`) as `created`,
    `application`.`name` as `application`,
    `mode`.`name` as `mode`,
    `template_content`.`subject` as `subject`,
    `template_content`.`body` as `body`
FROM `template` JOIN `target` ON `template`.`user_id`=`target`.`id`
LEFT OUTER JOIN `template_active` ON `template`.`id` = `template_active`.`template_id`
JOIN `template_content` ON `template`.`id` = `template_content`.`template_id`
JOIN `application` ON `template_content`.`application_id` = `application`.`id`
JOIN `mode` ON `template_content`.`mode_id` = `mode`.`id`'''

single_template_query_plans = '''SELECT
DISTINCT `plan_active`.`plan_id` as `id`, `plan_active`.`name` as `name`
FROM `plan_notification`
JOIN `plan_active` ON `plan_notification`.`plan_id` = `plan_active`.`plan_id`
WHERE `plan_notification`.`template` = %s'''

insert_plan_query = '''INSERT INTO `plan` (
    `user_id`, `name`, `created`, `description`, `step_count`,
    `threshold_window`, `threshold_count`, `aggregation_window`,
    `aggregation_reset`, `tracking_key`, `tracking_type`, `tracking_template`
) VALUES (
    (SELECT `id` FROM `target` where `name` = :creator),
    :name,
    :created,
    :description,
    :step_count,
    :threshold_window,
    :threshold_count,
    :aggregation_window,
    :aggregation_reset,
    :tracking_key,
    :tracking_type,
    :tracking_template
)'''

# FIXME: remove template_id column
insert_plan_step_query = '''INSERT INTO `plan_notification` (
    `plan_id`, `step`, `priority_id`, `target_id`, `template`, `role_id`, `repeat`, `wait`
) VALUES (
    :plan_id,
    :step,
    :priority_id,
    (SELECT `id` FROM `target` WHERE `name` = :target),
    :template,
    :role_id,
    :repeat,
    :wait
)'''

reprioritization_setting_query = '''SELECT
    `target`.`name` as `target`,
    `mode_src`.`name` as `src_mode`,
    `mode_dst`.`name` as `dst_mode`,
    `target_reprioritization`.`count` as `count`,
    `target_reprioritization`.`duration` as `duration`
FROM `target_reprioritization`
LEFT JOIN `target` ON `target`.`id` = `target_reprioritization`.`target_id`
LEFT JOIN `mode` `mode_src` ON `mode_src`.`id` = `target_reprioritization`.`src_mode_id`
LEFT JOIN `mode` `mode_dst` ON `mode_dst`.`id` = `target_reprioritization`.`dst_mode_id`
WHERE `target`.`name` = %s
'''

update_reprioritization_settings_query = '''INSERT INTO target_reprioritization (
    `target_id`, `src_mode_id`, `dst_mode_id`, `count`, `duration`
) VALUES (
    (SELECT `id` FROM `target` WHERE `name` = :target),
    :src_mode_id,
    :dst_mode_id,
    :count,
    :duration
) ON DUPLICATE KEY UPDATE `dst_mode_id`=:dst_mode_id,
                          `count`=:count,
                          `duration`=:duration'''

delete_reprioritization_settings_query = '''DELETE
FROM `target_reprioritization`
WHERE `target_id` = (SELECT `id` from `target` where `name` = :target_name)
      AND
      `src_mode_id` = (SELECT `id` from `mode` where `name` = :mode_name)'''

get_user_modes_query = '''SELECT
    `priority`.`name` as priority,
    `mode`.`name` as mode from `priority`
JOIN `target_mode` on `target_mode`.`priority_id` = `priority`.`id`
JOIN `mode` on `mode`.`id` = `target_mode`.`mode_id`
JOIN `target` on `target`.`id` =  `target_mode`.`target_id`
WHERE `target`.`name` = :username'''

get_target_application_modes_query = '''SELECT
    `priority`.`name` as priority,
    `mode`.`name` as mode from `priority`
JOIN `target_application_mode` on `target_application_mode`.`priority_id` = `priority`.`id`
JOIN `mode` on `mode`.`id` = `target_application_mode`.`mode_id`
JOIN `target` on `target`.`id` =  `target_application_mode`.`target_id`
JOIN `application` on `application`.`id` = `target_application_mode`.`application_id`
WHERE `target`.`name` = :username AND `application`.`name` = :app'''

insert_user_modes_query = '''INSERT
INTO `target_mode` (`priority_id`, `target_id`, `mode_id`)
VALUES (
    (SELECT `id` from `priority` WHERE `name` = :priority),
    (SELECT `id` from `target` WHERE `name` = :name),
    (SELECT `id` from `mode` WHERE `name` = :mode))
ON DUPLICATE KEY UPDATE
    `target_mode`.`mode_id` = (SELECT `id` from `mode` WHERE `name` = :mode)'''

delete_user_modes_query = '''DELETE FROM `target_mode`
WHERE `target_id` = (SELECT `id` from `target` WHERE `name` = :name)
      AND
      `priority_id` = (SELECT `id` from `priority` WHERE `name` = :priority)'''

insert_target_application_modes_query = '''INSERT
INTO `target_application_mode`
    (`priority_id`, `target_id`, `mode_id`, `application_id`)
VALUES (
    (SELECT `id` from `priority` WHERE `name` = :priority),
    (SELECT `id` from `target` WHERE `name` = :name),
    (SELECT `id` from `mode` WHERE `name` = :mode),
    (SELECT `id` from `application` WHERE `name` = :app))
ON DUPLICATE KEY UPDATE
    `target_application_mode`.`mode_id` = (SELECT `id` from `mode` WHERE `name` = :mode)'''

delete_target_application_modes_query = '''DELETE FROM `target_application_mode`
WHERE `target_id` = (SELECT `id` from `target` WHERE `name` = :name) AND
      `priority_id` = (SELECT `id` from `priority` WHERE `name` = :priority) AND
      `application_id` = (SELECT `id` from `application` WHERE `name` = :app)'''

get_applications_query = '''SELECT
    `id`, `name`, `context_template`, `sample_context`, `summary_template`
FROM `application`
WHERE `auth_only` is False'''

get_vars_query = 'SELECT `name`, `required` FROM `template_variable` WHERE `application_id` = %s ORDER BY `required` DESC, `name` ASC'

get_allowed_roles_query = '''SELECT `target_role`.`id`
                             FROM `target_role`
                             JOIN `target_type` ON `target_type`.`id` = `target_role`.`type_id`
                             JOIN `target` ON `target`.`type_id` = `target_type`.`id`
                             WHERE `target`.`name` = :target'''

uuid4hex = re.compile('[0-9a-f]{32}\Z', re.I)


def load_config_file(config_path):
    with open(config_path) as h:
        config = yaml.safe_load(h)

    if 'init_config_hook' in config:
        try:
            module = config['init_config_hook']
            logging.info('Bootstrapping config using %s' % module)
            getattr(import_module(module), module.split('.')[-1])(config)
        except ImportError:
            logger.exception('Failed loading config hook %s' % module)

    return config


def stream_incidents_with_context(cursor):
    for row in cursor:
        row['context'] = ujson.loads(row['context'])
        yield row


def get_app_from_msg_id(session, msg_id):
    sql = '''SELECT `application`.`name` FROM `message`
             JOIN `application` on `application`.`id` = `message`.`application_id`
             WHERE `message`.`id` = :msg_id'''
    result = session.execute(sql, {'msg_id': msg_id}).scalar()

    if result:
        return result
    else:
        return None


def is_valid_tracking_settings(t, k, tpl):
    if not t:
        if k or tpl:
            return False, 'Incomplete tracking setting'
        else:
            # no tracking setting
            return True, None
    if not (k and tpl):
        return False, 'Incomplete tracking setting'
    if not isinstance(tpl, dict) or not tpl:
        return False, 'Template must be a dictionary'
    if t == 'email':
        if '@' not in k:
            return False, 'Invalid email address'
        for app in tpl:
            if not tpl[app]:
                return False, 'No key for %s template' % app
            missed_keys = set(('email_subject', 'email_text')) - set(tpl[app])
            if missed_keys:
                return False, 'Missing keys for %s template: %s' % (app, missed_keys)
    else:
        return False, 'Unknown tracking type: %s' % t
    return True, None


def gen_where_filter_clause(connection, filters, filter_types, kwargs):
    where = []
    for key, values in kwargs.iteritems():
        col, _, op = key.partition('__')
        col_type = filter_types.get(col, str)
        # Format strings because Falcon splits on ',' but not on '%2C'
        # TODO: Get rid of this by setting request options on Falcon 1.1
        if isinstance(values, basestring):
            values = values.split(',')
        for val in values:
            if op == 'in':
                if len(values) == 1:
                    op = 'eq'
                    val = col_type(values[0])
                else:
                    val = tuple([col_type(v) for v in values])
            else:
                val = col_type(val)
            where.append(operators[op] % (filters[col], connection.escape(val)))
    return where


class HeaderMiddleware(object):
    def process_request(self, req, resp):
        resp.content_type = 'application/json'
        resp.set_headers([XFRAME, XCONTENTTYPEOPTIONS, XXSSPROTECTION])


class ReqBodyMiddleware(object):
    '''
    Falcon's req object has a stream that we read to obtain the post body.
    However, we can only read this once, and we often need the post body twice
    (once for authentication and once in the handler method). To avoid this
    problem, we read the post body into the request context and access it from
    there.

    IMPORTANT NOTE: Because we use stream.read() here, all other uses of this
    method will return '', not the post body.
    '''

    def process_request(self, req, resp):
        req.context['body'] = req.stream.read()


class AuthMiddleware(object):
    def __init__(self, debug=False):
        if debug:
            self.process_resource = self.debug_auth

    def debug_auth(self, req, resp, resource, params):
        try:
            app, client_digest = req.get_header('AUTHORIZATION', '')[5:].split(':', 1)
            if app not in cache.applications:
                raise HTTPUnauthorized('Authentication failure', 'Application not found', [])
            req.context['app'] = cache.applications[app]
        except TypeError:
            return

    def process_resource(self, req, resp, resource, params):  # pragma: no cover
        method = req.method
        if resource.allow_read_only and method == 'GET':
            return
        path = req.env['PATH_INFO']
        qs = req.env['QUERY_STRING']
        if qs:
            path = path + '?' + qs
        body = req.context['body']
        auth = req.get_header('AUTHORIZATION')
        if auth and auth.startswith('hmac '):
            try:
                app, client_digest = auth[5:].split(':', 1)
                app = cache.applications[app]
                api_key = str(app['key'])
                window = int(time.time()) // 5
                text = '%s %s %s %s' % (window, method, path, body)
                HMAC = hmac.new(api_key, text, hashlib.sha512)
                digest = base64.urlsafe_b64encode(HMAC.digest())
                if equals(client_digest, digest):
                    req.context['app'] = app
                    return
                else:
                    text = '%s %s %s %s' % (window - 1, method, path, body)
                    HMAC = hmac.new(api_key, text, hashlib.sha512)
                    digest = base64.urlsafe_b64encode(HMAC.digest())
                    if equals(client_digest, digest):
                        req.context['app'] = app
                    else:
                        raise HTTPUnauthorized('Authentication failure', '', [])

            except (ValueError, KeyError):
                raise HTTPUnauthorized('Authentication failure', '', [])

        else:
            raise HTTPUnauthorized('Authentication failure', '', [])


class Plan(object):
    allow_read_only = True

    def on_get(self, req, resp, plan_id):
        if plan_id.isdigit():
            where = 'WHERE `plan`.`id` = %s'
        else:
            where = 'WHERE `plan`.`name` = %s AND `plan_active`.`plan_id` IS NOT NULL'
        query = single_plan_query + where

        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(query, plan_id)
        results = cursor.fetchall()

        if results:
            plan = results[0]
            step = 0
            steps = []
            cursor.execute(single_plan_query_steps, plan['id'])
            for notification in cursor:
                s = notification['step']
                if s != step:
                    l = [notification]
                    steps.append(l)
                    step = s
                else:
                    l.append(notification)
            plan['steps'] = steps
            if plan['tracking_template']:
                plan['tracking_template'] = ujson.loads(plan['tracking_template'])

            payload = ujson.dumps(plan)
            connection.close()
        else:
            connection.close()
            raise HTTPNotFound()
        resp.status = HTTP_200
        resp.body = payload

    def on_post(self, req, resp, plan_id):
        session = db.Session()
        try:
            post_body = ujson.loads(req.context['body'])
            try:
                active = int(post_body['active'])
            except KeyError:
                raise HTTPBadRequest('"active" field required', '')
            except ValueError:
                raise HTTPBadRequest('Invalid active field', '')
            if active:
                session.execute('''INSERT INTO `plan_active` (`name`, `plan_id`)
                                   VALUES ((SELECT `name` FROM `plan` WHERE `id` = :plan_id), :plan_id)
                                   ON DUPLICATE KEY UPDATE `plan_id`=:plan_id''',
                                {'plan_id': plan_id})
            else:
                session.execute('DELETE FROM `plan_active` WHERE `plan_id`=:plan_id', {'plan_id': plan_id})
            session.commit()
            session.close()
            resp.status = HTTP_200
            resp.body = ujson.dumps(active)
        except HTTPBadRequest:
            raise
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Plans(object):
    allow_read_only = True

    def on_get(self, req, resp):
        '''
        Plan search endpoint.

        **Example request**:

        .. sourcecode:: http

           GET /api/v0/plans?name__contains=foo&active=1

        **Example response**:

        .. sourcecode:: http

           HTTP/1.1 200 OK
           Content-Type: application/json

           [
               {
                   "description": "This is plan foo",
                   "threshold_count": 10,
                   "creator": "user1",
                   "created": 1478154275,
                   "aggregation_reset": 300,
                   "aggregation_window": 300,
                   "threshold_window": 900,
                   "tracking_type": null,
                   "tracking_template": null,
                   "tracking_key": null,
                   "active": 1,
                   "id": 123456,
                   "name": "foo-sla0"
               }
           ]
        '''
        query_limit = req.get_param_as_int('limit')
        req.params.pop('limit', None)
        fields = req.get_param_as_list('fields')
        req.params.pop('fields', None)
        if fields is None:
            fields = plan_columns

        query = plan_query % ', '.join(plan_columns[f] for f in fields)

        where = []
        active = req.get_param_as_bool('active')
        req.params.pop('active', None)
        if active is not None:
            if active:
                where.append('`plan_active`.`plan_id` IS NOT NULL')
            else:
                where.append('`plan_active`.`plan_id` IS NULL')

        connection = db.engine.raw_connection()
        where += gen_where_filter_clause(
            connection, plan_filters, plan_filter_types, req.params)

        if where:
            query = query + ' WHERE ' + ' AND '.join(where)

        if query_limit is not None:
            query += ' ORDER BY `plan`.`created` DESC LIMIT %s' % query_limit

        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(query)

        payload = ujson.dumps(cursor)
        connection.close()
        resp.status = HTTP_200
        resp.body = payload

    def on_post(self, req, resp):
        plan_params = ujson.loads(req.context['body'])
        session = db.Session()
        try:
            run_validation('plan', plan_params)
            now = datetime.datetime.utcnow()
            plan_name = plan_params['name']
            # FIXME: catch creator not exist error

            tracking_key = plan_params.get('tracking_key')
            tracking_type = plan_params.get('tracking_type')
            tracking_template = plan_params.get('tracking_template')
            is_valid, err_msg = is_valid_tracking_settings(tracking_type, tracking_key, tracking_template)
            if not is_valid:
                raise HTTPBadRequest('Invalid tracking template', err_msg)

            if tracking_template:
                tracking_template = ujson.dumps(tracking_template)
            else:
                tracking_template = None  # in case tracking_template is an empty dict
            plan_dict = {
                'creator': plan_params['creator'],
                'name': plan_name,
                'created': now,
                'description': plan_params['description'],
                'step_count': len(plan_params['steps']),
                'threshold_window': plan_params['threshold_window'],
                'threshold_count': plan_params['threshold_count'],
                'aggregation_window': plan_params['aggregation_window'],
                'aggregation_reset': plan_params['aggregation_reset'],
                'tracking_key': tracking_key,
                'tracking_type': tracking_type,
                'tracking_template': tracking_template,
            }

            plan_id = session.execute(insert_plan_query, plan_dict).lastrowid

            for index, steps in enumerate(plan_params['steps'], start=1):
                for step in steps:
                    step['plan_id'] = plan_id
                    step['step'] = index
                    priority = cache.priorities.get(step['priority'])
                    role = cache.target_roles.get(step['role'])

                    if priority:
                        step['priority_id'] = priority['id']
                    else:
                        raise HTTPBadRequest('Invalid plan', 'Priority not found for step %s' % index)
                    if role:
                        step['role_id'] = role
                    else:
                        raise HTTPBadRequest('Invalid plan', 'Role not found for step %s' % index)

                    allowed_roles = {row[0] for row in session.execute(get_allowed_roles_query, step)}

                    if not allowed_roles:
                        raise HTTPBadRequest('Invalid plan', 'Target %s not found for step %s' % (step['target'], index))

                    if role not in allowed_roles:
                        raise HTTPBadRequest('Invalid role', 'Role %s is not appropriate for target %s in step %s' % (
                                             step['role'], step['target'], index))

                    try:
                        session.execute(insert_plan_step_query, step)
                    except IntegrityError:
                        raise HTTPBadRequest('Invalid plan', 'Target not found for step %s' % index)

            session.execute('INSERT INTO `plan_active` (`name`, `plan_id`) '
                            'VALUES (:name, :plan_id) ON DUPLICATE KEY UPDATE `plan_id`=:plan_id',
                            {'name': plan_name, 'plan_id': plan_id})

            session.commit()
            session.close()
            resp.status = HTTP_201
            resp.body = ujson.dumps(plan_id)
            resp.set_header('Location', '/plans/%s' % plan_id)
        except IrisValidationException as e:
            session.close()
            raise HTTPBadRequest('Validation error', str(e))
        except HTTPBadRequest:
            raise
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Incidents(object):
    allow_read_only = True

    def on_get(self, req, resp):
        fields = req.get_param_as_list('fields')
        if fields is None:
            fields = incident_columns
        req.params.pop('fields', None)
        query_limit = req.get_param_as_int('limit')
        req.params.pop('limit', None)
        target = req.get_param_as_list('target')
        req.params.pop('target', None)

        query = incident_query % ', '.join(incident_columns[f] for f in fields)

        connection = db.engine.raw_connection()
        where = gen_where_filter_clause(connection, incident_filters, incident_filter_types, req.params)
        sql_values = []
        if target:
            where.append('''`incident`.`id` IN (
                SELECT `incident_id`
                FROM `message`
                JOIN `target` ON `message`.`target_id`=`target`.`id`
                WHERE `target`.`name` IN %s
            )''')
            sql_values.append(tuple(target))
        if where:
            query = query + ' WHERE ' + ' AND '.join(where)
        if query_limit is not None:
            query += ' ORDER BY `incident`.`created` DESC LIMIT %s' % query_limit

        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(query, sql_values)

        if 'context' in fields:
            payload = ujson.dumps(stream_incidents_with_context(cursor))
        else:
            payload = ujson.dumps(cursor)
        connection.close()
        resp.status = HTTP_200
        resp.body = payload

    def on_post(self, req, resp):
        session = db.Session()
        incident_params = ujson.loads(req.context['body'])
        if 'plan' not in incident_params:
            session.close()
            raise HTTPBadRequest('missing plan name attribute', '')

        plan_id = session.execute('SELECT `plan_id` FROM `plan_active` WHERE `name` = :plan',
                                  {'plan': incident_params['plan']}).scalar()
        if not plan_id:
            logger.warn('Plan "%s" not found.', incident_params['plan'])
            session.close()
            raise HTTPNotFound()

        app = req.context['app']

        if 'application' in incident_params:
            if not req.context['app']['allow_other_app_incidents']:
                raise HTTPForbidden('This application does not allow creating incidents as other applications', '')

            app = cache.applications.get(incident_params['application'])

            if not app:
                raise HTTPBadRequest('Invalid application', '')

        try:
            context = incident_params['context']
            context_json_str = ujson.dumps({variable: context.get(variable)
                                           for variable in app['variables']})
            if len(context_json_str) > 65535:
                raise HTTPBadRequest('Context too long', '')

            app_template_count = session.execute('''
                SELECT EXISTS (
                  SELECT 1 FROM
                  `plan_notification`
                  JOIN `template` ON `template`.`name` = `plan_notification`.`template`
                  JOIN `template_content` ON `template_content`.`template_id` = `template`.`id`
                  WHERE `plan_notification`.`plan_id` = :plan_id
                  AND `template_content`.`application_id` = :app_id
                )
            ''', {'app_id': app['id'], 'plan_id': plan_id}).scalar()

            if not app_template_count:
                raise HTTPBadRequest('No plan template actions exist for this app', '')

            data = {
                'plan_id': plan_id,
                'created': datetime.datetime.utcnow(),
                'application_id': app['id'],
                'context': context_json_str,
                'current_step': 0,
                'active': True,
            }

            incident_id = session.execute(
                '''INSERT INTO `incident` (`plan_id`, `created`, `context`, `current_step`, `active`, `application_id`)
                   VALUES (:plan_id, :created, :context, 0, :active, :application_id)''',
                data).lastrowid

            session.commit()
            session.close()
            resp.status = HTTP_201
            resp.set_header('Location', '/incidents/%s' % incident_id)
            resp.body = ujson.dumps(incident_id)
        except HTTPBadRequest:
            raise
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Incident(object):
    allow_read_only = True

    def on_get(self, req, resp, incident_id):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        try:
            cursor.execute(single_incident_query, int(incident_id))
        except ValueError:
            raise HTTPBadRequest('Invalid incident id', '')
        results = cursor.fetchall()

        if results:
            incident = results[0]
            cursor.execute(single_incident_query_steps, (auditlog.MODE_CHANGE, auditlog.TARGET_CHANGE, incident['id']))
            incident['steps'] = cursor.fetchall()
            connection.close()

            incident['context'] = ujson.loads(incident['context'])
            payload = ujson.dumps(incident)
        else:
            connection.close()
            raise HTTPNotFound()
        resp.status = HTTP_200
        resp.body = payload

    def on_post(self, req, resp, incident_id):
        session = db.Session()
        incident_params = ujson.loads(req.context['body'])
        try:
            owner = incident_params['owner']
            is_active = utils.claim_incident(incident_id, owner, session)
            resp.status = HTTP_200
            resp.body = ujson.dumps({'incident_id': int(incident_id),
                                     'owner': owner,
                                     'active': is_active})
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Message(object):
    allow_read_only = True

    def on_get(self, req, resp, message_id):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(single_message_query, int(message_id))
        results = cursor.fetchall()
        connection.close()
        if results:
            payload = ujson.dumps(results[0])
        else:
            raise HTTPNotFound()
        resp.status = HTTP_200
        resp.body = payload


class MessageAuditLog(object):
    allow_read_only = True

    def on_get(self, req, resp, message_id):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(message_audit_log_query, int(message_id))
        results = cursor.fetchall()
        connection.close()
        if results:
            payload = ujson.dumps(results)
        else:
            raise HTTPNotFound()
        resp.status = HTTP_200
        resp.body = payload


class Messages(object):
    allow_read_only = True

    def on_get(self, req, resp):
        fields = req.get_param_as_list('fields')
        if fields is None:
            fields = message_columns
        req.params.pop('fields', None)
        query_limit = req.get_param_as_int('limit')
        req.params.pop('limit', None)

        connection = db.engine.raw_connection()
        escaped_params = {
          'mode_change': connection.escape(auditlog.MODE_CHANGE),
          'target_change': connection.escape(auditlog.TARGET_CHANGE)
        }

        query = message_query % ', '.join(message_columns[f] % escaped_params for f in fields)

        where = gen_where_filter_clause(connection, message_filters, message_filter_types, req.params)
        if where:
            query = query + ' WHERE ' + ' AND '.join(where)

        if query_limit is not None:
            query += ' ORDER BY `message`.`created` DESC LIMIT %s' % query_limit
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(query)
        payload = ujson.dumps(cursor)
        connection.close()
        resp.status = HTTP_200
        resp.body = payload


class Notifications(object):
    allow_read_only = False
    required_attrs = frozenset(['target', 'role', 'subject'])

    def __init__(self, config):
        self.sender_addr = (config['sender']['host'], config['sender']['port'])

    def on_post(self, req, resp):
        message = ujson.loads(req.context['body'])
        msg_attrs = set(message)
        if not msg_attrs >= self.required_attrs:
            raise HTTPBadRequest('Missing required atrributes',
                                 ', '.join(self.required_attrs - msg_attrs))

        # If both priority and mode are passed in, priority overrides mode
        if 'priority' in message:
            priority = cache.priorities.get(message['priority'])
            if not priority:
                raise HTTPBadRequest('Invalid priority', message['priority'])
            message['priority_id'] = priority['id']
        elif 'mode' in message:
            mode_id = cache.modes.get(message['mode'])
            if not mode_id:
                raise HTTPBadRequest('Invalid mode', message['mode'])
            message['mode_id'] = mode_id
        else:
            raise HTTPBadRequest(
                'Both priority and mode are missing, at least one of it is required', '')

        message['application'] = req.context['app']['name']
        s = socket.create_connection(self.sender_addr)
        s.send(msgpack.packb({'endpoint': 'v0/send', 'data': message}))
        sender_resp = utils.msgpack_unpack_msg_from_socket(s)
        s.close()
        if sender_resp == 'OK':
            resp.status = HTTP_200
            resp.body = '[]'
        else:
            raise HTTPBadRequest('Request rejected by sender', sender_resp)


class Template(object):
    allow_read_only = True

    def on_get(self, req, resp, template_id):
            if template_id.isdigit():
                where = 'WHERE `template`.`id` = %s'
            else:
                where = 'WHERE `template`.`name` = %s AND `template_active`.`template_id` IS NOT NULL'
            query = single_template_query + where

            connection = db.engine.raw_connection()
            cursor = connection.cursor()
            cursor.execute(query, template_id)
            results = cursor.fetchall()

            if results:
                r = results[0]
                t = {
                    'id': r[0],
                    'name': r[1],
                    'active': r[2],
                    'creator': r[3],
                    'created': r[4]
                }
                content = {}
                for r in results:
                    content.setdefault(r[5], {})[r[6]] = {'subject': r[7], 'body': r[8]}
                t['content'] = content
                cursor = connection.cursor(db.dict_cursor)
                cursor.execute(single_template_query_plans, t['name'])
                t['plans'] = cursor.fetchall()
                connection.close()
                payload = ujson.dumps(t)
            else:
                raise HTTPNotFound()
            resp.status = HTTP_200
            resp.body = payload

    def on_post(self, req, resp, template_id):
        session = db.Session()
        template_params = ujson.loads(req.context['body'])
        try:
            try:
                active = int(template_params['active'])
            except ValueError:
                raise HTTPBadRequest('Invalid active argument', 'active must be an int')
            except KeyError:
                raise HTTPBadRequest('Missing active argument', '')
            if active:
                session.execute('''INSERT INTO `template_active` (`name`, `template_id`)
                                   VALUES ((SELECT `name` FROM `template` WHERE `id` = :template_id), :template_id)
                                   ON DUPLICATE KEY UPDATE `template_id`=:template_id''',
                                {'template_id': template_id})
            else:
                session.execute('DELETE FROM `template_active` WHERE `template_id`=:template_id',
                                {'template_id': template_id})
            session.commit()
            session.close()
            resp.status = HTTP_200
            resp.body = ujson.dumps(active)
        except HTTPBadRequest:
            raise
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Templates(object):
    allow_read_only = True

    def on_get(self, req, resp):
        query_limit = req.get_param_as_int('limit')
        req.params.pop('limit', None)
        fields = req.get_param_as_list('fields')
        if fields is None:
            fields = template_columns
        req.params.pop('fields', None)

        query = template_query % ', '.join(template_columns[f] for f in fields)

        where = []
        active = req.get_param_as_bool('active')
        req.params.pop('active', None)
        if active is not None:
            if active:
                where.append('`template_active`.`template_id` IS NOT NULL')
            else:
                where.append('`template_active`.`template_id` IS NULL')

        connection = db.engine.raw_connection()
        where += gen_where_filter_clause(connection, template_filters, template_filter_types, req.params)

        if where:
            query = query + ' WHERE ' + ' AND '.join(where)

        if query_limit is not None:
            query += ' ORDER BY `template`.`created` DESC LIMIT %s' % query_limit

        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(query)

        payload = ujson.dumps(cursor)
        connection.close()
        resp.status = HTTP_200
        resp.body = payload

    def on_post(self, req, resp):
        session = db.Session()
        try:
            template_params = ujson.loads(req.context['body'])
            if 'content' not in template_params:
                raise HTTPBadRequest('content argument missing', '')
            if 'name' not in template_params:
                raise HTTPBadRequest('name argument missing', '')
            if 'creator' not in template_params:
                raise HTTPBadRequest('creator argument missing', '')

            content = template_params.pop('content')
            contents = []
            template_env = SandboxedEnvironment(autoescape=True)
            for _application, modes in content.iteritems():
                for _mode, _content in modes.iteritems():
                    _content['mode'] = _mode
                    _content['application'] = _application
                    try:
                        template_env.from_string(_content['subject'])
                        template_env.from_string(_content['body'])
                    except jinja2.TemplateSyntaxError as e:
                        logger.exception('Invalid jinja syntax')
                        raise HTTPBadRequest('Invalid jinja template', str(e))
                    contents.append(_content)

            template_id = session.execute(
                ('INSERT INTO `template` (`name`, `created`, `user_id`) '
                 'VALUES (:name, now(), (SELECT `id` from `target` where `name` = :creator))'),
                template_params).lastrowid

            for _content in contents:
                _content.update({'template_id': template_id})
                session.execute('''INSERT INTO `template_content` (`template_id`, `subject`, `body`, `application_id`, `mode_id`)
                                   VALUES (
                                     :template_id, :subject, :body,
                                     (SELECT `id` FROM `application` WHERE `name` = :application),
                                     (SELECT `id` FROM `mode` WHERE `name` = :mode)
                                   )''', _content)

            session.execute('''INSERT INTO `template_active` (`name`, `template_id`)
                               VALUES (:name, :template_id)
                               ON DUPLICATE KEY UPDATE `template_id`=:template_id''',
                            {'name': template_params['name'], 'template_id': template_id})
            session.commit()
            session.close()
        except HTTPBadRequest:
            raise
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise

        resp.status = HTTP_201
        resp.set_header('Location', '/templates/%s' % template_id)
        resp.body = ujson.dumps(template_id)


class UserModes(object):
    allow_read_only = False

    def on_get(self, req, resp, username):
        session = db.Session()
        try:
            results = session.execute('SELECT `name` FROM `priority`')
            modes = {name: 'default' for (name, ) in results}

            app = req.get_param('application')
            if app is None:
                result = session.execute(get_user_modes_query, {'username': username})
            else:
                result = session.execute(get_target_application_modes_query, {'username': username, 'app': app})
            modes.update(list(result))

            session.close()
            resp.status = HTTP_200
            resp.body = ujson.dumps(modes)
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise

    # TODO (dewang): change to PUT for consistency with oncall
    def on_post(self, req, resp, username):
        session = db.Session()
        mode_params = ujson.loads(req.context['body'])
        try:
            results = session.execute('SELECT `name` FROM `priority`')
            modes = {name: 'default' for (name, ) in results}

            app = mode_params.pop('application', None)

            if app is None:
                for p, m in mode_params.iteritems():
                    if m != 'default':
                        session.execute(insert_user_modes_query, {'name': username, 'priority': p, 'mode': m})
                    else:
                        session.execute(delete_user_modes_query, {'name': username, 'priority': p})
                result = session.execute(get_user_modes_query, {'username': username})
            else:
                for p, m in mode_params.iteritems():
                    if m != 'default':
                        session.execute(insert_target_application_modes_query,
                                        {'name': username, 'priority': p, 'mode': m, 'app': app})
                    else:
                        session.execute(delete_target_application_modes_query,
                                        {'name': username, 'priority': p, 'app': app})
                result = session.execute(get_target_application_modes_query, {'username': username, 'app': app})

            modes.update(list(result))
            session.commit()
            session.close()
            resp.status = HTTP_200
            resp.body = ujson.dumps(modes)
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Targets(object):
    allow_read_only = False

    def on_get(self, req, resp):
        session = db.Session()
        filters_sql = []
        try:
            if 'startswith' in req.params:
                req.params['startswith'] = req.params['startswith'] + '%'
                filters_sql.append('`name` like :startswith')

            sql = '''SELECT `name` FROM `target`'''

            if filters_sql:
                sql += ' WHERE %s' % ' AND '.join(filters_sql)

            results = session.execute(sql, req.params)

            payload = ujson.dumps([row for (row,) in results])
            session.close()
            resp.status = HTTP_200
            resp.body = payload
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Target(object):
    allow_read_only = False

    def on_get(self, req, resp, target_type):
        session = db.Session()
        filters_sql = []
        try:
            req.params['type_id'] = cache.target_types[target_type]
            filters_sql.append('`type_id` = :type_id')

            if 'startswith' in req.params:
                req.params['startswith'] = req.params['startswith'] + '%'
                filters_sql.append('`name` like :startswith')

            sql = '''SELECT `name` FROM `target`'''

            if filters_sql:
                sql += ' WHERE %s' % ' AND '.join(filters_sql)

            results = session.execute(sql, req.params)

            payload = ujson.dumps([row for (row,) in results])
            session.close()
            resp.status = HTTP_200
            resp.body = payload
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise


class Application(object):
    allow_read_only = True

    def on_get(self, req, resp, app_name):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        app_query = get_applications_query + " AND `application`.`name` = %s"
        cursor.execute(app_query, app_name)
        app = cursor.fetchone()
        cursor.execute(get_vars_query, app['id'])
        app['variables'] = []
        app['required_variables'] = []
        for row in cursor:
            app['variables'].append(row['name'])
            if row['required']:
                app['required_variables'].append(row['name'])
        cursor.close()
        connection.close()

        del app['id']
        payload = app
        resp.status = HTTP_200
        resp.body = ujson.dumps(payload)


class Applications(object):
    allow_read_only = True

    def on_get(self, req, resp):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(get_applications_query)
        apps = cursor.fetchall()
        for app in apps:
            cursor.execute(get_vars_query, app['id'])
            app['variables'] = []
            app['required_variables'] = []
            for row in cursor:
                app['variables'].append(row['name'])
                if row['required']:
                    app['required_variables'].append(row['name'])
            del app['id']
        payload = apps
        cursor.close()
        connection.close()
        resp.status = HTTP_200
        resp.body = ujson.dumps(payload)


class Modes(object):
    allow_read_only = False

    def on_get(self, req, resp):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        mode_query = 'SELECT `id`,`name` FROM `mode`'''
        cursor.execute(mode_query)
        results = cursor.fetchall()
        cursor.close()
        connection.close()
        payload = [r['name'] for r in results]
        resp.status = HTTP_200
        resp.body = ujson.dumps(payload)


class Priorities(object):
    allow_read_only = False

    def on_get(self, req, resp):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        mode_query = ''' SELECT `priority`.`id`, `priority`.`name` AS `name`, `mode`.`name` AS `default_mode`
                         FROM `priority` JOIN `mode` ON `priority`.`mode_id` = `mode`.`id`'''
        mode_query += ' ORDER BY `priority`.`id` ASC'

        cursor.execute(mode_query)
        payload = ujson.dumps([{'name': r['name'], 'default_mode': r['default_mode']} for r in cursor])
        cursor.close()
        connection.close()
        resp.status = HTTP_200
        resp.body = payload


class User(object):
    allow_read_only = False

    def on_get(self, req, resp, user_id):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        # Get user id/name
        user_query = ''' SELECT `id`, `name` FROM `target`'''
        if user_id.isdigit():
            user_query += ' WHERE `id` = %s'
        else:
            user_query += ' WHERE `name` = %s'
        cursor.execute(user_query, user_id)
        if cursor.rowcount != 1:
            raise HTTPNotFound()
        user_data = cursor.fetchone()
        user_id = user_data.pop('id')

        # Get user contact modes
        modes_query = '''SELECT `priority`.`name` AS priority, `mode`.`name` AS `mode`
                         FROM `target` JOIN `target_mode` ON `target`.`id` = `target_mode`.`target_id`
                             JOIN `priority` ON `priority`.`id` = `target_mode`.`priority_id`
                             JOIN `mode` ON `mode`.`id` = `target_mode`.`mode_id`
                         WHERE `target`.`id` = %s'''
        cursor.execute(modes_query, user_id)
        user_data['modes'] = {}
        for row in cursor:
            user_data['modes'][row['priority']] = row['mode']

        # Get user teams
        teams_query = '''SELECT `target`.`name` AS `team`
                        FROM `user_team` JOIN `target` ON `user_team`.`team_id` = `target`.`id`
                        WHERE `user_team`.`user_id` = %s'''
        cursor.execute(teams_query, user_id)
        user_data['teams'] = []
        for row in cursor:
            user_data['teams'].append(row['team'])

        # Get user contact info
        contacts_query = '''SELECT `mode`.`name` AS `mode`, `target_contact`.`destination` AS `destination`
                            FROM `target_contact` JOIN `mode` ON `target_contact`.`mode_id` = `mode`.`id`
                            WHERE `target_contact`.`target_id` = %s'''
        cursor.execute(contacts_query, user_id)
        user_data['contacts'] = {}
        for row in cursor:
            user_data['contacts'][row['mode']] = row['destination']
        cursor.close()
        connection.close()
        resp.status = HTTP_200
        resp.body = ujson.dumps(user_data)


class ResponseMixin(object):
    allow_read_only = False

    def create_response(self, msg_id, source, content):
        """
        Return the result of the insert
        """
        session = db.Session()
        try:
            response_dict = {
                'source': source,
                'message_id': msg_id,
                'content': content,
            }
            result = session.execute('''
              INSERT INTO `response` (`source`, `message_id`, `content`, `created`)
              VALUES (:source, :message_id, :content, now())
            ''', response_dict)
            session.commit()
            session.close()
            return result
        except Exception as e:
            session.close()
            logger.exception('Failed to create response', e)
            raise

    def create_email_message(self, application, dest, subject, body):
        if application not in cache.applications:
            return False, 'Application "%s" not found in %s.' % (application, cache.applications.keys())

        app = cache.applications[application]

        session = db.Session()
        try:

            sql = '''SELECT `target`.`id` FROM `target`
                     JOIN `target_contact` on `target_contact`.`target_id` = `target`.`id`
                     JOIN `mode` on `mode`.`id` = `target_contact`.`mode_id`
                     WHERE `mode`.`name` = 'email' AND `target_contact`.`destination` = :destination'''
            target_id = session.execute(sql, {'destination': dest}).scalar()
            if not target_id:
                session.close()
                msg = 'Failed to lookup target from destination: %s' % dest
                logger.warn(msg)
                raise HTTPBadRequest('Invalid request', msg)

            data = {
                'created': datetime.datetime.utcnow(),
                'application_id': app['id'],
                'subject': subject,
                'target_id': target_id,
                'body': body,
                'destination': dest
            }

            sql = '''INSERT INTO `message` (`created`, `application_id`, `subject`, `target_id`, `body`, `destination`, `mode_id`, `priority_id`)
                     VALUES (:created, :application_id, :subject, :target_id, :body, :destination,
                      (SELECT `id` FROM `mode` WHERE `name` = 'email'),
                      (SELECT `id` FROM `priority` WHERE `name` = 'low')
                     )'''
            message_id = session.execute(sql, data).lastrowid

            session.commit()
            session.close()
            return True, message_id
        except Exception:
            session.close()
            logger.exception('ERROR')
            raise

    def handle_user_response(self, mode, msg_id, source, content):
        '''
        Insert user response into database, return:
            1. message id for user response
            2. forward plugin returns to caller
        '''
        def validate_app(app):
            if not app:
                msg = "Invalid message({0}): no application found.".format(msg_id)
                logger.exception(msg)
                raise HTTPBadRequest(msg, msg)

        session = db.Session()
        is_batch = False
        if isinstance(msg_id, int) or msg_id.isdigit():
            # FIXME: return error if message not found for id
            app = get_app_from_msg_id(session, msg_id)
            validate_app(app)
            self.create_response(msg_id, source, content)
        elif uuid4hex.match(msg_id):
            # msg id is not pure digit, might be a batch id
            sql = 'SELECT message.id FROM message WHERE message.batch=:batch_id'
            results = session.execute(sql, {'batch_id': msg_id})
            mid_lst = [row[0] for row in results]
            if len(mid_lst) < 1:
                raise HTTPBadRequest('Invalid message id', 'invalid message id: %s' % msg_id)

            # assuming message batching is also keyed on app, so they are from
            # the same app
            app = get_app_from_msg_id(session, mid_lst[0])
            validate_app(app)
            for mid in mid_lst:
                self.create_response(mid, source, content)
            is_batch = True
        else:
            raise HTTPBadRequest('Invalid message id', 'invalid message id: %s' % msg_id.encode('utf-8'))

        try:
            resp = find_plugin(app).handle_response(
                mode, msg_id, source, content, batch=is_batch)
        except Exception as e:
            raise HTTPBadRequest('Failed to handle response', 'failed to handle response: %s' % str(e))
        session.close()
        return app, resp


class ResponseGmail(ResponseMixin):
    def on_post(self, req, resp):
        source = None
        subject = None
        gmail_params = ujson.loads(req.context['body'])
        # TODO(khrichar): there has to be a better way
        for h in gmail_params['headers']:
            key = h.get('name')
            if key == 'From':
                source = h.get('value')
            elif key == 'Subject':
                subject = h.get('value')
        if not source:
            msg = 'No source found in headers: %s' % gmail_params['headers']
            raise HTTPBadRequest('Missing source', msg)
        # source is in the format of "First Last <user@email.com>",
        # but we only want the email part
        source = source.split(' ')[-1].strip('<>'),

        # only parse first line of email content for now
        content = gmail_params['body']
        first_line = content.strip().split('\n', 1)[0].strip()
        try:
            msg_id, cmd = utils.parse_email_response(first_line, subject, source)
        except (ValueError, IndexError):
            raise HTTPBadRequest('Invalid response', 'Invalid response: %s' % first_line)

        try:
            app, response = self.handle_user_response('email', msg_id, source, cmd)
        except Exception:
            logger.exception('Failed to handle email response: %s' % first_line)
            raise
        else:
            success, re = self.create_email_message(app, source, 'Re: %s' % subject, response)
            if not success:
                logger.error('Failed to send user response email: %s' % re)
                raise HTTPBadRequest('Failed to send user response email', re)
            resp.status = HTTP_204


class ResponseGmailOneClick(ResponseMixin):
    def on_post(self, req, resp):
        gmail_params = ujson.loads(req.context['body'])

        try:
            msg_id = gmail_params['msg_id']
            email_address = gmail_params['email_address']
            cmd = gmail_params['cmd']
        except KeyError:
            raise HTTPBadRequest('Post body missing required key', '')

        try:
            app, response = self.handle_user_response('email', msg_id, email_address, cmd)
        except Exception:
            logger.exception('Failed to handle gmail one click response: %s' % gmail_params)
            raise

        success, re = self.create_email_message(app, email_address, response, response)
        if not success:
            logger.error('Failed to send user response email: %s' % re)
            raise HTTPBadRequest('Failed to send user response email', re)
        resp.status = HTTP_204


class ResponseTwilioCalls(ResponseMixin):
    def on_post(self, req, resp):
        post_dict = parse_qs(req.context['body'])

        msg_id = req.get_param('message_id', required=True)
        if 'Digits' not in post_dict:
            raise HTTPBadRequest('Digits argument not found', '')
        # For phone call callbacks, To argument is the target and From is the
        # twilio number
        if 'To' not in post_dict:
            raise HTTPBadRequest('To argument not found', '')
        digits = post_dict['Digits'][0]
        source = post_dict['To'][0]

        try:
            _, response = self.handle_user_response('call', msg_id, source, digits)
        except Exception:
            logger.exception('Failed to handle call response: %s' % digits)
            raise
        else:
            resp.status = HTTP_200
            resp.body = ujson.dumps({'app_response': response})


class ResponseTwilioMessages(ResponseMixin):
    def on_post(self, req, resp):
        post_dict = parse_qs(req.context['body'])
        if 'Body' not in post_dict:
            raise HTTPBadRequest('SMS body not found', 'Missing Body argument in post body')

        if 'From' not in post_dict:
            raise HTTPBadRequest('From argument not found', 'Missing From in post body')
        source = post_dict['From'][0]
        body = post_dict['Body'][0]
        try:
            msg_id, content = utils.parse_response(body.strip(), 'sms', source)
        except (ValueError, IndexError):
            raise HTTPBadRequest('Invalid response', 'failed to parse response')

        try:
            _, response = self.handle_user_response('sms', msg_id, source, content)
        except Exception:
            logger.exception('Failed to handle sms response: %s' % body)
            raise
        else:
            resp.status = HTTP_200
            resp.body = ujson.dumps({'app_response': response})


class Reprioritization(object):
    allow_read_only = False

    def on_get(self, req, resp, target_name):
        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        cursor.execute(reprioritization_setting_query, target_name)
        settings = cursor.fetchall()
        cursor.close()
        connection.close()
        resp.status = HTTP_200
        resp.body = ujson.dumps(settings)

    def on_post(self, req, resp, target_name):
        params = ujson.loads(req.context['body'])
        required_args = ['duration', 'count', 'src_mode', 'dst_mode']
        # Check for required arguments
        for arg in required_args:
            if arg not in params:
                raise HTTPBadRequest('Missing argument', 'missing arg: %s' % arg)

        # Validate duration/count
        try:
            duration = int(params['duration'])
        except ValueError:
            raise HTTPBadRequest('Invalid duration', 'duration must be an integer')
        if duration < 60:
            raise HTTPBadRequest('Invalid duration', 'duration must be greater than 1 minute')
        if duration > 3600:
            raise HTTPBadRequest('Invalid duration', 'duration must be at most 3600 seconds')
        try:
            count = int(params['count'])
        except ValueError:
            raise HTTPBadRequest('Invalid count', 'count must be an integer')
        if count <= 0:
            raise HTTPBadRequest('Invalid count', 'count must be greater than 0')
        if count > 255:
            raise HTTPBadRequest('Invalid count', 'count must be under 255')

        connection = db.engine.raw_connection()
        cursor = connection.cursor(db.dict_cursor)
        try:
            cursor.execute('SELECT `id` FROM `mode` WHERE `name` = %s', params['src_mode'])
            src_mode_id = cursor.fetchone()['id']
        except:
            msg = 'Invalid source mode.'
            logger.exception(msg)
            raise HTTPBadRequest(msg, msg)
        try:
            cursor.execute('SELECT `id` FROM `mode` WHERE `name` = %s', params['dst_mode'])
            dst_mode_id = cursor.fetchone()['id']
        except:
            msg = 'Invalid destination mode.'
            logger.exception(msg)
            raise HTTPBadRequest(msg, msg)
        cursor.close()

        session = db.Session()
        session.execute(update_reprioritization_settings_query, {
            'target': target_name,
            'src_mode_id': src_mode_id,
            'dst_mode_id': dst_mode_id,
            'count': count,
            'duration': duration,
        })
        session.commit()
        session.close()
        resp.status = HTTP_200
        resp.body = '[]'


class ReprioritizationMode(object):
    allow_read_only = False

    def on_delete(self, req, resp, target_name, src_mode_name):
        session = db.Session()
        affected_rows = session.execute(delete_reprioritization_settings_query, {
          'target_name': target_name,
          'mode_name': src_mode_name,
        }).rowcount
        session.commit()
        session.close()

        if affected_rows == 0:
            raise HTTPNotFound()

        resp.status = HTTP_200
        resp.body = '[]'


class Healthcheck(object):
    allow_read_only = True

    def __init__(self, path):
        self.healthcheck_path = path

    def on_get(self, req, resp):
        try:
            with open(self.healthcheck_path) as f:
                health = f.readline().strip()
        except:
            raise HTTPNotFound()
        resp.status = HTTP_200
        resp.content_type = 'text/plain'
        resp.body = health


class Stats(object):
    allow_read_only = True

    def on_get(self, req, resp):
        queries = {
          'total_plans': 'SELECT COUNT(*) FROM `plan`',
          'total_incidents': 'SELECT COUNT(*) FROM `incident`',
          'total_messages_sent': 'SELECT COUNT(*) FROM `message`',
          'total_incidents_today': 'SELECT COUNT(*) FROM `incident` WHERE `created` >= CURDATE()',
          'total_messages_sent_today': 'SELECT COUNT(*) FROM `message` WHERE `sent` >= CURDATE()',
          'total_active_users': 'SELECT COUNT(*) FROM `target` WHERE `type_id` = (SELECT `id` FROM `target_type` WHERE `name` = "user") AND `active` = TRUE',
        }

        stats = {}

        session = db.Session()
        for key, query in queries.iteritems():
            result = session.execute(query).scalar()
            stats[key] = result

        session.close()
        resp.status = HTTP_200
        resp.body = ujson.dumps(stats)


def get_api_app():
    import sys
    config = load_config_file(sys.argv[1])
    logging.basicConfig()
    return get_api(config)


def get_api(config):
    db.init(config)
    cache.init()
    init_plugins(config.get('plugins', {}))
    init_validators(config.get('validators', []))
    healthcheck_path = config['healthcheck_path']

    debug = False
    if config['server'].get('disable_auth'):
        debug = True
    req = ReqBodyMiddleware()
    header = HeaderMiddleware()
    auth = AuthMiddleware(debug=debug)
    middleware = [req, auth, header]

    app = API(middleware=middleware)

    app.add_route('/v0/plans/{plan_id}', Plan())
    app.add_route('/v0/plans', Plans())

    app.add_route('/v0/incidents/{incident_id}', Incident())
    app.add_route('/v0/incidents', Incidents())

    app.add_route('/v0/messages/{message_id}', Message())
    app.add_route('/v0/messages/{message_id}/auditlog', MessageAuditLog())
    app.add_route('/v0/messages', Messages())

    app.add_route('/v0/notifications', Notifications(config))

    app.add_route('/v0/targets/{target_type}', Target())
    app.add_route('/v0/targets', Targets())

    app.add_route('/v0/templates/{template_id}', Template())
    app.add_route('/v0/templates', Templates())

    app.add_route('/v0/users/{user_id}', User())
    app.add_route('/v0/users/modes/{username}', UserModes())
    app.add_route('/v0/users/reprioritization/{target_name}', Reprioritization())
    app.add_route('/v0/users/reprioritization/{target_name}/{src_mode_name}', ReprioritizationMode())

    app.add_route('/v0/modes', Modes())

    app.add_route('/v0/applications/{app_name}', Application())
    app.add_route('/v0/applications', Applications())

    app.add_route('/v0/priorities', Priorities())

    app.add_route('/v0/response/gmail', ResponseGmail())
    app.add_route('/v0/response/gmail-oneclick', ResponseGmailOneClick())
    app.add_route('/v0/response/twilio/calls', ResponseTwilioCalls())
    app.add_route('/v0/response/twilio/messages', ResponseTwilioMessages())

    app.add_route('/v0/stats', Stats())

    app.add_route('/healthcheck', Healthcheck(healthcheck_path))

    return app
