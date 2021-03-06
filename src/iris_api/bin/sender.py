# Copyright (c) LinkedIn Corporation. All rights reserved. Licensed under the BSD-2 Clause license.
# See LICENSE in the project root for license information.

from gevent import monkey, sleep, spawn, queue
monkey.patch_all()

import logging
import sys
import time
import ujson

from collections import defaultdict
from iris_api.plugins import init_plugins
from iris_api.vendors import init_vendors, send_message
from iris_api.sender import auditlog
from iris_api.metrics import stats, init as init_metrics, emit_metrics
from uuid import uuid4
from iris_api.gmail import Gmail
from iris_api import db
from iris_api.api import load_config_file
from iris_api.sender import rpc, cache
from iris_api.sender.message import update_message_mode
from iris_api.sender.oneclick import oneclick_email_markup, generate_oneclick_url
from iris_api import cache as api_cache

# sql

NEW_INCIDENTS = '''SELECT
    `incident`.`id` as `id`,
    `incident`.`plan_id` as `plan_id`,
    `incident`.`context` as `context`,
    `application`.`name` as `application`
FROM `incident`
JOIN `application`
ON `incident`.`application_id`=`application`.`id`
WHERE `current_step`=0 AND `active`=1'''

INACTIVE_SQL = '''UPDATE
`incident`
SET `active`=0
WHERE `id` IN (
    SELECT distinct `incident_id`
    FROM (
        SELECT
        `incident_id`,
        `max`,
        `age`,
        MAX(`count`) as max_count
        FROM (
            SELECT
            `message`.`incident_id` as `incident_id`,
            `message`.`plan_notification_id` as `plan_notification_id`,
            count(`message`.`id`) as `count`,
            `plan_notification`.`repeat` + 1 as `max`,
            TIMESTAMPDIFF(SECOND, MAX(`message`.`sent`), NOW()) as `age`,
            `plan_notification`.`wait` as `wait`,
            `plan_notification`.`step` as `step`,
            `incident`.`current_step`,
            `plan`.`step_count`,
            `message`.`plan_id`,
            `message`.`application_id`,
            `incident`.`context`
            FROM `message`
            JOIN `incident` ON `message`.`incident_id` = `incident`.`id`
            JOIN `plan_notification` ON `message`.`plan_notification_id` = `plan_notification`.`id`
            JOIN `plan` ON `message`.`plan_id` = `plan`.`id`
            WHERE `incident`.`active` = 1
            AND `incident`.`current_step`=`plan`.`step_count`
            AND `step` = `incident`.`current_step`
            GROUP BY `incident`.`id`, `message`.`plan_notification_id`, `message`.`target_id`
        ) as `inner`
        GROUP BY `incident_id`, `plan_notification_id`
        HAVING `max_count` = `max` AND BIT_AND(`age` > `wait`) = 1
    ) as `exhausted_incidents`
)'''

QUEUE_SQL = '''SELECT
`incident_id`,
`plan_id`,
`plan_notification_id`,
max(`count`) as `count`,
`max`,
`age`,
`wait`,
`step`,
`current_step`,
`step_count`
FROM (
    SELECT
        `message`.`incident_id` as `incident_id`,
        `message`.`plan_notification_id` as `plan_notification_id`,
        count(`message`.`id`) as `count`,
        `plan_notification`.`repeat` + 1 as `max`,
        TIMESTAMPDIFF(SECOND, max(`message`.`created`), NOW()) as `age`,
        `plan_notification`.`wait` as `wait`,
        `plan_notification`.`step` as `step`,
        `incident`.`current_step`,
        `plan`.`step_count`,
        `message`.`plan_id`,
        `message`.`application_id`,
        `incident`.`context`
    FROM `message`
    JOIN `incident` ON `message`.`incident_id` = `incident`.`id`
    JOIN `plan_notification` ON `message`.`plan_notification_id` = `plan_notification`.`id`
    JOIN `plan` ON `message`.`plan_id` = `plan`.`id`
    WHERE `incident`.`active` = 1
    GROUP BY `incident`.`id`, `message`.`plan_notification_id`, `message`.`target_id`
) as `inner`
GROUP BY `incident_id`, `plan_notification_id`
HAVING `age` > `wait` AND (`count` < `max`
                           OR (`count` = `max` AND `step` = `current_step`
                               AND `step` < `step_count`))'''

UPDATE_INCIDENT_SQL = '''UPDATE `incident` SET `current_step`=%s WHERE `id`=%s'''

INVALIDATE_INCIDENT = '''UPDATE `incident` SET `active`=0 WHERE `id`=%s'''

INSERT_MESSAGE_SQL = '''INSERT INTO `message`
    (`created`, `plan_id`, `plan_notification_id`, `incident_id`, `application_id`, `target_id`, `priority_id`, `body`)
VALUES (NOW(), %s,%s,%s,%s,%s,%s,%s)'''

UNSENT_MESSAGES_SQL = '''SELECT
    `message`.`body`,
    `message`.`id` as `message_id`,
    `target`.`name` as `target`,
    `priority`.`name` as `priority`,
    `priority`.`id` as `priority_id`,
    `application`.`name` as `application`,
    `plan`.`name` as `plan`,
    `plan`.`id` as `plan_id`,
    `incident`.`id` as `incident_id`,
    `incident`.`context` as `context`,
    `plan_notification`.`template` as `template`
FROM `message`
JOIN `application` ON `message`.`application_id`=`application`.`id`
JOIN `priority` ON `message`.`priority_id`=`priority`.`id`
JOIN `target` ON `message`.`target_id`=`target`.`id`
LEFT OUTER JOIN `plan` ON `message`.`plan_id`=`plan`.`id`
LEFT OUTER JOIN `plan_notification` ON `message`.`plan_notification_id`=`plan_notification`.`id`
LEFT OUTER JOIN `incident` ON `message`.`incident_id`=`incident`.`id`
WHERE `message`.`active`=1'''

SENT_MESSAGE_BATCH_SQL = '''UPDATE `message`
SET `destination`=%%s,
    `mode_id`=%%s,
    `template_id`=%%s,
    `subject`=%%s,
    `body`=%%s,
    `batch`=%%s,
    `active`=FALSE,
    `sent`=NOW()
WHERE `id` IN %s'''

SENT_MESSAGE_SQL = '''UPDATE `message`
SET `destination`=%s,
    `mode_id`=%s,
    `template_id`=%s,
    `subject`=%s,
    `body`=%s,
    `active`=FALSE,
    `sent`=NOW()
WHERE `id`=%s'''

PRUNE_OLD_AUDIT_LOGS_SQL = '''DELETE FROM `message_changelog` WHERE `date` < DATE_SUB(CURDATE(), INTERVAL 3 MONTH)'''

# logging

logger = logging.getLogger()
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(formatter)
logger.setLevel(logging.INFO)
logger.addHandler(ch)


# rate limiting data structure message key -> minute -> count
# used to calcuate if a new message exceeds the rate limit
# and needs to be queued
windows = {}

# all messages waiting to be queue across all keys
messages = {}

# queue of message_ids waiting to be sent for a given key
queues = {}

# time last message was queued while in aggregation mode for a given key
# used to determine if a new message should be aggregated or if aggregation should end
aggregation = {}

# last time a batch was sent for a given key
# used to determine if it's time to send the next batch
sent = {}

# queue for messages entering the system
# this sets the ground work for not having to poll the DB for messages
message_queue = queue.Queue()

# queue for sending messages
from iris_api.sender.shared import send_queue

default_sender_metrics = {
    'email_cnt': 0, 'email_total': 0, 'email_fail': 0, 'email_sent': 0, 'email_max': 0,
    'email_min': 0, 'email_avg': 0, 'im_cnt': 0, 'im_total': 0, 'im_fail': 0, 'im_sent': 0,
    'im_max': 0, 'im_min': 0, 'im_avg': 0, 'sms_cnt': 0, 'sms_total': 0, 'sms_fail': 0,
    'sms_sent': 0, 'sms_max': 0, 'sms_min': 0, 'sms_avg': 0, 'call_cnt': 0, 'call_total': 0,
    'call_fail': 0, 'call_sent': 0, 'call_max': 0, 'call_min': 0, 'call_avg': 0, 'task_failure': 0,
    'oncall_error': 0, 'role_target_lookup_error': 0, 'target_not_found': 0, 'message_send_cnt': 0,
    'notification_cnt': 0, 'api_request_cnt': 0, 'api_request_timeout_cnt': 0,
    'rpc_message_pass_success_cnt': 0, 'rpc_message_pass_fail_cnt': 0,
    'slave_message_send_success_cnt': 0, 'slave_message_send_fail_cnt': 0
}

# TODO: make this configurable
target_fallback_mode = 'email'
should_mock_gwatch_renewer = False
config = None


def create_messages(incident_id, plan_notification_id):
    application_id = cache.incidents[incident_id]['application_id']
    plan_notification = cache.plan_notifications[plan_notification_id]
    role = cache.roles[plan_notification['role_id']]['name']
    target = cache.targets[plan_notification['target_id']]['name']
    # find role/priority from plan_notification_id
    names = cache.targets_for_role(role, target)
    priority_id = plan_notification['priority_id']
    changed_target = False
    body = ''

    if not names:
        stats['role_target_lookup_error'] += 1

        # Try to get creator of the plan and nag them instead
        name = None
        try:
            name = cache.plans[plan_notification['plan_id']]['creator']
        except (KeyError, TypeError):
            pass

        if not name:
            logger.error(('Failed to find targets for incident %s, plan_notification_id: %s, role: %s, target: %s, result: %s and failed looking '
                          'up the plan\'s creator'), incident_id, plan_notification_id, role, target, names)
            return False

        try:
            priority_id = api_cache.priorities['low']['id']
        except KeyError:
            logger.error(('Failed to find targets for incident %s, plan_notification_id: %s, role: %s, target: %s, result: %s and failed looking '
                          'up ID for low priority'), incident_id, plan_notification_id, role, target, names)
            return False

        logger.error(('Failed to find targets for incident %s, plan_notification_id: %s, role: %s, target: %s, result: %s. '
                      'Reaching out to %s instead and lowering priority to low (%s)'),
                     incident_id, plan_notification_id, role, target, names, name, priority_id)

        body = 'You are receiving this as you created this plan and we can\'t resolve %s of %s at this time.\n\n' % (role, target)
        names = [name]
        changed_target = True

    connection = db.engine.raw_connection()
    cursor = connection.cursor()

    for name in names:
        t = cache.target_names[name]
        if t:
            target_id = t['id']
            cursor.execute(INSERT_MESSAGE_SQL,
                           (plan_notification['plan_id'], plan_notification_id, incident_id,
                            application_id, target_id, priority_id, body))

            if changed_target:
                connection.commit()  # needed for the lastrowid to exist in the DB to satsify the constraint
                auditlog.message_change(cursor.lastrowid, auditlog.TARGET_CHANGE, role + '|' + target, name,
                                        'Changing target as we failed resolving original target')

        else:
            stats['target_not_found'] += 1
            logger.error('No target found: %s', name)

    connection.commit()
    cursor.close()
    connection.close()
    return True


def deactivate():
    # deactivate incidents that have expired
    logger.info('[-] start deactivate task...')
    start_deactivation = time.time()

    connection = db.engine.raw_connection()
    cursor = connection.cursor()
    cursor.execute(INACTIVE_SQL)
    connection.commit()
    cursor.close()
    connection.close()

    stats['deactivation'] = time.time() - start_deactivation
    logger.info('[*] deactivate task finished')


def escalate():
    # make notifications for things that should repeat or escalate
    logger.info('[-] start escalate task...')

    # first, handle new incidents
    start_notifications = time.time()

    connection = db.engine.raw_connection()
    cursor = connection.cursor()
    cursor.execute(NEW_INCIDENTS)

    escalations = {}
    for incident_id, plan_id, context, application in cursor:
        escalations[incident_id] = (plan_id, 1)
        # create tracking message if configured
        plan = cache.plans[plan_id]
        tracking_type = plan['tracking_type']
        tracking_key = plan['tracking_key']
        tracking_template = plan['tracking_template']
        app_tracking_template = tracking_template.get(application) if tracking_template else None
        if tracking_type and tracking_key and app_tracking_template:
            # plan defines tracking notifications
            context = ujson.loads(context)
            context['iris'] = {
                'incident_id': incident_id,
                'plan': plan['name'],
                'plan_id': plan_id,
                'application': application,
            }
            if tracking_type == 'email':
                tracking_message = {
                    'noreply': True,
                    'destination': tracking_key,
                    'mode': tracking_type
                }

                try:
                    subject = app_tracking_template['email_subject'].render(**context)
                except Exception, e:
                    subject = 'plan %s - tracking notification subject failed to render: %s' % (plan['name'], str(e))
                    logger.exception(subject)
                tracking_message['email_subject'] = subject

                try:
                    body = app_tracking_template['email_text'].render(**context)
                except Exception, e:
                    body = 'plan %s - tracking notification body failed to render: %s' % (plan['name'], str(e))
                    logger.exception(body)
                tracking_message['email_text'] = body

                email_html_tpl = app_tracking_template.get('email_html')
                if email_html_tpl:
                    try:
                        html_body = email_html_tpl.render(**context)
                    except Exception, e:
                        html_body = 'plan %s - tracking notification html body failed to render: %s' % (plan['name'], str(e))
                        logger.exception(html_body)
                    tracking_message['email_html'] = html_body

                spawn(send_message, tracking_message)
    cursor.close()
    logger.info('[*] %s new incidents', len(escalations))

    # then, fetch message count for current incidents
    msg_count = 0
    cursor = connection.cursor(db.dict_cursor)
    cursor.execute(QUEUE_SQL)
    for n in cursor.fetchall():
        if n['count'] < n['max']:
            if create_messages(n['incident_id'], n['plan_notification_id']):
                msg_count += 1
        else:
            escalations[n['incident_id']] = (n['plan_id'], n['current_step'] + 1)

    for incident_id, (plan_id, step) in escalations.iteritems():
        plan = cache.plans[plan_id]
        steps = plan['steps'].get(step, [])
        if steps:
            step_msg_cnt = 0
            for plan_notification_id in steps:
                if create_messages(incident_id, plan_notification_id):
                    step_msg_cnt += 1
            if step == 1 and step_msg_cnt == 0:
                # no message created due to role look up failure, reset step to
                # 0 for retry
                step = 0
            cursor.execute(UPDATE_INCIDENT_SQL, (step, incident_id))
            msg_count += step_msg_cnt
        else:
            logger.error('plan id %d has no steps, incident id %d is invalid', plan_id, incident_id)
            cursor.execute(INVALIDATE_INCIDENT, incident_id)
        connection.commit()
    cursor.close()
    connection.close()

    logger.info('[*] %s new messages', msg_count)
    logger.info('[*] escalate task finished')
    stats['notifications'] = time.time() - start_notifications


def aggregate(now):
    # see if it's time to send the batches
    logger.info('[-] start aggregate task - queued: %s', len(messages))
    start_aggregations = time.time()
    for key in queues.keys():
        aggregation_window = cache.plans[key[0]]['aggregation_window']
        if now - sent.get(key, 0) >= aggregation_window:
            aggregated_message_ids = queues[key]

            connection = db.engine.raw_connection()
            cursor = connection.cursor()
            cursor.execute('SELECT `id` FROM `message` WHERE active=1 AND `id` in %s', [aggregated_message_ids])
            active_message_ids = {r[0] for r in cursor}
            cursor.close()
            connection.close()

            inactive_message_ids = aggregated_message_ids - active_message_ids
            l = len(active_message_ids)
            logger.info('[x] dropped %s messages from claimed incidents, %s remain for %r',
                        len(inactive_message_ids), l, key)

            # remove inactive message from the queue
            for message_id in inactive_message_ids:
                del messages[message_id]

            if l == 1:
                m = messages.pop(next(iter(active_message_ids)))
                logger.info('aggregate - %(message_id)s pushing to send queue', m)
                send_queue.put(m)
            elif l > 1:
                uuid = uuid4().hex
                m = messages[next(iter(active_message_ids))]
                logger.info('aggregate - %s pushing to send queue', uuid)
                m['batch_id'] = uuid

                # Cast from set to list, as sets are not msgpack serializable
                m['aggregated_ids'] = list(active_message_ids)
                send_queue.put(m)
                for message_id in active_message_ids:
                    del messages[message_id]
                logger.info('[-] purged %s from messages %s remaining', active_message_ids, len(messages))
            del queues[key]
            sent[key] = now
    stats['aggregations'] = time.time() - start_aggregations
    logger.info('[*] aggregate task finished - queued: %s', len(messages))


def poll():
    # poll unsent messages
    logger.info('[-] start send task...')
    start_send = time.time()

    connection = db.engine.raw_connection()
    cursor = connection.cursor(db.dict_cursor)
    if messages:
        cursor.execute(UNSENT_MESSAGES_SQL + ' AND `message`.`id` NOT IN %s', [tuple(messages)])
    else:
        cursor.execute(UNSENT_MESSAGES_SQL)

    new_msg_count = cursor.rowcount
    queued_msg_cnt = len(messages)
    stats['new_msg_count'] = new_msg_count
    logger.info('%d new messages waiting in database - queued: %d', new_msg_count, queued_msg_cnt)

    for m in cursor:
        # iris's own email response does not have context since content and
        # subject are already set
        if m.get('context'):
            context = ujson.loads(m['context'])
            # inject meta variables
            context['iris'] = {k: m[k] for k in m if k != 'context'}
            m['context'] = context
        message_queue.put(m)

    stats['poll'] = time.time() - start_send
    stats['queue'] = len(messages)
    logger.info('[*] send task finished')
    cursor.close()
    connection.close()


def fetch_and_prepare_message():
    now = time.time()
    m = message_queue.get()
    message_id = m['message_id']
    plan_id = m['plan_id']
    if plan_id is None:
        send_queue.put(m)
        return

    plan = cache.plans[plan_id]

    # queue key
    key = (m['plan_id'], m['application'], m['priority'], m['target'])

    # should this message be aggregated?
    aggregate = False
    last_aggregation = aggregation.get(key)
    if last_aggregation:
        if now - last_aggregation > plan['aggregation_reset']:
            # it's been long enough since the last message
            # return to immediate sending mode and clear aggregations
            del aggregation[key]
            try:
                # if we have sent batches before delete the entry for last sent
                del sent[key]
            except KeyError:
                pass
        else:
            # still getting enough messages fast enough to remain in aggregation
            aggregation[key] = now
            aggregate = True

    if aggregate:
        # we are still in a previous aggregation mode
        queues.setdefault(key, set()).add(message_id)
        messages[message_id] = m
    else:
        # does this message trigger aggregation?
        window = windows.setdefault(key, defaultdict(int))

        for bucket in window.keys():
            if now - bucket > plan['threshold_window']:
                del window[bucket]

        window[now] += 1

        if sum(window.itervalues()) > plan['threshold_count']:
            # too many messages for the aggregation key - enqueue

            # add message id to aggregation queue
            queues[key] = set([message_id])
            # add message id to queue for deduping
            messages[message_id] = m
            # initialize last sent tracker
            sent[key] = now
            # initialize aggregation indicator
            aggregation[key] = now
            # TODO: also render message content here?
            audit_msg = 'Aggregated with key %s' % (key,)
            spawn(auditlog.message_change, m['message_id'], auditlog.SENT_CHANGE, '', '', audit_msg)
        else:
            # cleared for immediate sending
            send_queue.put(m)


def send():
    logger.info('[-] start send loop...')
    while True:
        fetch_and_prepare_message()
    logger.info('[*] send loop finished...')


# sender stat adder
def add_stat(mode, runtime):
    stats[mode + '_cnt'] += 1
    if runtime is None:
        stats[mode + '_fail'] += 1
    else:
        stats[mode + '_total'] += runtime
        stats[mode + '_sent'] += 1
        if runtime > stats[mode + '_max']:
            stats[mode + '_max'] = runtime
        elif runtime < stats[mode + '_min']:
            stats[mode + '_min'] = runtime
        if runtime < stats[mode + '_min']:
            stats[mode + '_min'] = runtime
        elif runtime > stats[mode + '_max']:
            stats[mode + '_max'] = runtime


def set_target_fallback_mode(message):
    try:
        connection = db.engine.raw_connection()
        cursor = connection.cursor()
        cursor.execute('''SELECT `destination`, `mode`.`name`, `mode`.`id`
                          FROM `target`
                          JOIN `target_contact` ON `target_contact`.`target_id` = `target`.`id`
                          JOIN `mode` ON `mode`.`id` = `target_contact`.`mode_id`
                          WHERE `target`.`name` = %s AND `mode`.`name` = %s''',
                       (message['target'], target_fallback_mode))
        [(destination, mode, mode_id)] = cursor
        cursor.close()
        connection.close()

        message['destination'] = destination
        message['mode'] = mode
        message['mode_id'] = mode_id
        return True
    # target doesn't have email either - bail
    except ValueError:
        logger.error('target does not have mode(%s) %r', target_fallback_mode, message)
        message['destination'] = message['mode'] = message['mode_id'] = None
        return False


def set_target_contact_by_priority(message):
    connection = db.engine.raw_connection()
    cursor = connection.cursor()
    cursor.execute('''
        SELECT `destination`, `mode`.`name`, `mode`.`id`
        FROM `target` JOIN `target_contact` ON `target_contact`.`target_id` = `target`.`id`
        JOIN `mode` ON `mode`.`id` = `target_contact`.`mode_id`
        WHERE `target`.`name` = %s AND `target_contact`.`mode_id` = IFNULL(
            -- 1. lookup per application user setting
            (
                SELECT `target_application_mode`.`mode_id`
                FROM `target_application_mode`
                JOIN `application` ON `target_application_mode`.`application_id` = `application`.`id`
                WHERE `target_application_mode`.`target_id` = `target`.`id` AND
                        `application`.`name` = %s AND
                        `target_application_mode`.`priority_id` = %s
            ), IFNULL(
            -- 2. lookup default user setting
                (
                    SELECT `target_mode`.`mode_id`
                    FROM `target_mode`
                    WHERE `target_mode`.`target_id` = `target`.`id` AND
                            `target_mode`.`priority_id` = %s
                ), (
            -- 3. lookup default iris setting
                    SELECT `mode_id`
                    FROM `priority`
                    WHERE `id` = %s
                )
            )
        )''', (message['target'], message['application'], message['priority_id'],
               message['priority_id'], message['priority_id']))
    [(destination, mode, mode_id)] = cursor
    cursor.close()
    connection.close()

    message['destination'] = destination
    message['mode'] = mode
    message['mode_id'] = mode_id


def set_target_contact(message):
    try:
        if 'mode' in message:
            # for out of band notification, we already have the mode and
            # mode_id set by API
            connection = db.engine.raw_connection()
            cursor = connection.cursor()
            cursor.execute('''
                SELECT `destination` FROM `target_contact`
                JOIN `target` ON `target`.`id` = `target_contact`.`target_id`
                WHERE `target`.`name` = %s AND `target_contact`.`mode_id` = %s;
                ''', (message['target'], message['mode_id']))
            message['destination'] = cursor.fetchone()[0]
            cursor.close()
            connection.close()
        else:
            # message triggered by incident will only have priority
            set_target_contact_by_priority(message)
        cache.target_reprioritization(message)
        return True
    except ValueError:
        logger.error('target does not have mode %r', message)
        return set_target_fallback_mode(message)


def render(message):
    if not message.get('template'):
        if message.get('message_id'):
            # email response from iris does not use template this means the
            # message content is already in DB
            connection = db.engine.raw_connection()
            cursor = connection.cursor(db.dict_cursor)
            cursor.execute('SELECT `subject`, `body` FROM `message` WHERE `id` = %s',
                           message['message_id'])
            msg_content = cursor.fetchone()
            message['body'], message['subject'] = msg_content['body'], msg_content['subject']
            cursor.close()
            connection.close()
        else:
            # out of band message does not have id and should already have the
            # content populated
            return
    elif 'aggregated_ids' in message:
        message['subject'] = '[%%(application)s] %s messages from plan %%(plan)s' % len(message['aggregated_ids']) % message
        message['body'] = 'Batch ID: %(batch_id)s' % message
        message['template_id'] = None
    else:
        if message['body'] is None:
            message['body'] = ''
        error = None
        try:
            template = cache.templates[message['template']]
            try:
                application_template = template[message['application']]
                try:
                    mode_template = application_template[message['mode']]
                    try:
                        message['subject'] = mode_template['subject'].render(**message['context'])
                    except Exception as e:
                        error = 'template %(template)s - %(application)s - %(mode)s - subject failed to render: ' + str(e)
                    try:
                        message['body'] += mode_template['body'].render(**message['context'])
                    except Exception as e:
                        error = 'template %(template)s - %(application)s - %(mode)s - body failed to render: ' + str(e)
                    message['template_id'] = template['id']
                except KeyError:
                    error = 'template %(template)s - %(application)s does not have mode %(mode)s'
            except KeyError:
                error = 'template %(template)s does not have application %(application)s'
        except KeyError:
            error = 'template %(template)s does not exist'

        if error:
            # TODO:
            # define application default template here
            # additionally, instead of repr() as the final message render
            # format define a generic template that will work for all contexts
            # - even those that are invalid as a final final format maybe repr or pprint
            logger.error(error, message)
            message['subject'] = '%(message_id)s Iris failed to render your message' % message
            message['body'] = 'Failed rendering message.\n\nContext: %s\n\nError: %s' % (repr(message), error % message)
            message['template_id'] = None
        else:

            if config.get('enable_gmail_oneclick') and message['mode'] == 'email' and 'incident_id' in message:
                oneclick_url = generate_oneclick_url(config, {
                    'msg_id': message['message_id'],
                    'email_address': message['destination'],
                    'cmd': 'claim'
                })
                additional_body = oneclick_email_markup % {
                    'url': oneclick_url,
                    'incident_id': message['incident_id']
                }
                message['extra_html'] = additional_body
                logger.info('Added oneclick URL metadata to extra_html key of message %s', message['message_id'])


def mark_message_as_sent(message):
    connection = db.engine.raw_connection()
    params = [
        message['destination'],
        message['mode_id'],
        message.get('template_id'),
        message['subject'],
        message['body'],
    ]
    if 'aggregated_ids' in message:
        sql = SENT_MESSAGE_BATCH_SQL % connection.escape(message['aggregated_ids'])
        params.append(message['batch_id'])
    else:
        sql = SENT_MESSAGE_SQL
        params.append(message['message_id'])
    cursor = connection.cursor()
    if not message['subject']:
        message['subject'] = ''
        logger.warn('Message id %s has blank subject', message.get('message_id', '?'))
    if len(message['subject']) > 255:
        message['subject'] = message['subject'][:255]
    cursor.execute(sql, params)
    connection.commit()
    cursor.close()
    connection.close()


def mark_message_has_no_contact(message):
    message_id = message.get('message_id')
    if not message_id:
        logger.warn('Cannot mark message "%s" as not having contact as message_id is missing', message)
        return

    connection = db.engine.raw_connection()
    cursor = connection.cursor()
    cursor.execute('UPDATE `message` set `active`=0 WHERE `id`=%s',
                   message_id)
    connection.commit()
    cursor.close()
    connection.close()
    auditlog.message_change(
        message_id, auditlog.MODE_CHANGE, target_fallback_mode, 'invalid',
        'Ignore message as we failed to resolve target contact')


def distributed_send_message(message):
    if rpc.num_slaves and rpc.sender_slaves:
        for i, address in enumerate(rpc.sender_slaves):
            if i >= rpc.num_slaves:
                logger.error('Failed using all configured slaves; resorting to local send_message')
                break
            if rpc.send_message_to_slave(message, address):
                return True

    logger.info('Sending message (ID %s) locally', message.get('message_id', '?'))

    runtime = send_message(message)
    add_stat(message['mode'], runtime)
    if runtime is not None:
        return True

    raise Exception('Failed sending message')


def fetch_and_send_message():
    message = send_queue.get()
    has_contact = set_target_contact(message)
    if not has_contact:
        mark_message_has_no_contact(message)
        return

    if 'message_id' not in message:
        message['message_id'] = None

    render(message)
    success = None
    try:
        success = distributed_send_message(message)
    except Exception:
        logger.exception('Failed to send message: %s', message)
        if message['mode'] == 'email':
            logger.error('unable to send %(mode)s %(message_id)s %(application)s %(destination)s %(subject)s %(body)s', message)
            stats['task_failure'] += 1
        else:
            logger.error('reclassifying as email %(mode)s %(message_id)s %(application)s %(destination)s %(subject)s %(body)s', message)
            old_mode = message['mode']
            if (set_target_fallback_mode(message)):
                update_message_mode(message)
                auditlog.message_change(
                    message['message_id'], auditlog.MODE_CHANGE, old_mode, message['mode'],
                    'Changing mode due to original mode failure')
            render(message)
            try:
                success = distributed_send_message(message)
            # nope - log and bail
            except Exception:
                stats['task_failure'] += 1
                logger.error('unable to send %(mode)s %(message_id)s %(application)s %(destination)s %(subject)s %(body)s', message)
    if success:
        stats['message_send_cnt'] += 1
        if message['message_id']:
            mark_message_as_sent(message)


def worker():
    while True:
        fetch_and_send_message()


def gwatch_renewer():
    gmail_config = config['gmail']
    gcli = Gmail(gmail_config, config.get('gmail_proxy'))
    while True:
        logger.info('[-] start gmail watcher loop...')
        logger.info('renewing gmail watcher...')
        re = gcli.watch(gmail_config['project'], gmail_config['topic'])
        try:
            history_id, expiration = (int(re['historyId']),
                                      int(re['expiration']) / 1000 - time.time())
        except KeyError:
            logger.exception('[*] gmail watcher run failed. Skipping this run.')
        else:
            stats['gmail_history_id'] = history_id
            stats['gmail_seconds_to_watch_expiration'] = expiration
            logger.info('[*] gmail watcher loop finished')

        # only renew every 8 hours
        sleep(60*60*8)


def prune_old_audit_logs_worker():
    while True:
        connection = db.engine.raw_connection()
        cursor = connection.cursor()
        cursor.execute(PRUNE_OLD_AUDIT_LOGS_SQL)
        connection.commit()
        cursor.close()
        connection.close()
        logger.info('Ran task to prune old audit logs. Waiting 4 hours until next run.')
        sleep(60*60*4)


def mock_gwatch_renewer():
    while True:
        logger.info('[-] start mock gmail watcher loop...')
        logger.info('[*] mock gmail watcher loop finished')
        sleep(60)


def init_sender(config):
    db.init(config)
    cache.init(config)
    init_metrics(config, 'iris-sender', default_sender_metrics)

    global should_mock_gwatch_renewer, send_message
    if config['sender'].get('debug'):
        logger.info('DEBUG MODE')
        should_mock_gwatch_renewer = True
        should_skip_send = True
    else:
        should_skip_send = False
    should_mock_gwatch_renewer = should_mock_gwatch_renewer or config.get('skipgmailwatch', False)
    should_skip_send = should_skip_send or config.get('skipsend', False)

    if should_skip_send:
        config['vendors'] = [{
          'type': 'iris_dummy',
          'name': 'iris dummy vendor'
        }]


def main():
    if len(sys.argv) <= 1:
        print 'ERROR: missing config file.'
        print 'usage: %s API_CONFIG_FILE' % sys.argv[0]
        sys.exit(1)

    global config
    config = load_config_file(sys.argv[1])

    is_master = config['sender'].get('is_master', False)
    logger.info('[-] bootstraping sender (master: %s)...', is_master)
    init_sender(config)
    init_plugins(config.get('plugins', {}))
    init_vendors(config.get('vendors', []), config.get('applications', []))
    api_cache.cache_priorities()

    send_task = spawn(send)
    worker_tasks = [spawn(worker) for x in xrange(100)]
    if is_master:
        if should_mock_gwatch_renewer:
            spawn(mock_gwatch_renewer)
        else:
            spawn(gwatch_renewer)
        spawn(prune_old_audit_logs_worker)

    rpc.init(config['sender'], dict(send_message=send_message, add_stat=add_stat))
    rpc.run(config['sender'])

    interval = 60
    logger.info('[*] sender bootstrapped')
    while True:
        runtime = int(time.time())
        logger.info('--> sender looop started.')

        cache.refresh()
        cache.purge()

        if is_master:
            try:
                escalate()
                deactivate()
                poll()
                aggregate(runtime)
            except Exception:
                stats['task_failure'] += 1
                logger.exception("Exception occured in main loop.")

        # check status for all background greenlets and respawn if necessary
        if not bool(send_task):
            logger.error("send task failed, %s", send_task.exception)
            stats['task_failure'] += 1
            send_task = spawn(send)
        bad_workers = []
        for i, task in enumerate(worker_tasks):
            if not bool(task):
                logger.error("worker task failed, %s", task.exception)
                stats['task_failure'] += 1
                bad_workers.append(i)
        for i in bad_workers:
            worker_tasks[i] = spawn(worker)

        spawn(emit_metrics)

        now = time.time()
        elapsed_time = now - runtime
        nap_time = max(0, interval - elapsed_time)
        logger.info('--> sender loop finished in %s seconds - sleeping %s seconds',
                    elapsed_time, nap_time)
        sleep(nap_time)


if __name__ == '__main__':
    main()
