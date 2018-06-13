#!/usr/bin/python
# -*- coding: utf-8
#

import os
import sys
import argparse
import json
import re
from datetime import datetime, timedelta
import time
import urllib2
import ConfigParser
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urlparse import urlparse, urljoin


PROG = os.path.basename(sys.argv[0]).rstrip('.py')
PROG_DESC = 'Send alert notification for redmine issues'

DEFAULT_CONFIG = {
    'mail': {
        'to':   'test@domain.local',
        'from': 'test@domain.local',
        'host': 'smtp.domain.local',
        'user': 'test@domain.local',
        'password': 'password',
        'port': 465,
        'subject': 'TEST'
    },
    'redmine': {
        'url': 'http://localhost',
        'api-key': 'key',
    },
}

MAIL_SUBJECT = {
    'duedate': '[WARNING] Issues without due date',
    'duetime': '[ALARM] Critial opened issues',
    'info': '[INFO]'
}

DEFAULT_PARAMS = {'sort': 'id', 'limit': 100}
RM_DATE_FMT = '%Y-%m-%dT%H:%M:%SZ'
FILTER_RM_DATE_FMT = '{}{:%Y-%m-%dT%H:%M:%SZ}'

DATA_HEAD = {
    'head': '',
    'project': '',
    'sla': '',
    'desc': '',
    'id': "id",
    'subject': "Subject",
    'priority': "Priority",
    'created_on': "Created",
    'info': "Expired"
}

DATA_BODY = {
    'url': '',
    'id': 0,
    'subject': '',
    'priority': '',
    'created_on': '',
    'info':  ''
}


REPORT_HEAD_FORMAT=u'''{head:=^80}
{project:^80}
{sla:^80}
{desc:-^80}
{id:>5s} | {subject:32.32s} | {priority:6.6s} | {created_on:11s} | {info}'''
REPORT_DATA_FORMAT=u'{id:5d} | {subject:32.32s} | {priority:6.6s} | {created_on:%d/%m %H:%M} | {info}'
REPORT_FOOT_FORMAT=u'{:=^80}'

HTML_HEAD_FORMAT=u'''<P>
<UL>
<LI>Project: {project}</LI>
<LI>SLA: {sla}</LI>
<LI>{desc}</LI>
</UL>
<TABLE>
<TR><TH>{subject:s}</TH><TH>{priority:s}</TH><TH>{created_on:s}</TH><TH>{info}</TH></TR>
'''
HTML_HEAD_FORMAT2=u'<TABLE><TR><TH>{subject:s}</TH><TH>{priority:s}</TH><TH>{created_on:s}</TH><TH>{info}</TH></TR>\n'
HTML_DATA_FORMAT=u"""<TR><TD><A href='{url}/issues/{id:d}'><B>#{id:d}</B></A> {subject:s}</TD><TD>{priority:s}</TD><TD>{created_on:%Y-%m-%d %H:%M}</TD><TD>{info}</TD></TR>\n"""
HTML_FOOT_FORMAT=u'</TABLE><BR>\n'
HTML_FORMAT = """
<html>
<head>
<style>
table {{ border-collapse: collapse; }}
th, td {{
    border: 1px solid gray;
    padding-right: 6px;
    padding-left: 6px;
}}
a {{text-decoration: none}}
</style>
</head>
<body>
{DATA}
</body></html>
"""

SLA = {
    '24x7': 'Problems with New status more than 2 hours',
    '5x8': 'Problems with New status more than 1 day'
}


def json_dump(j):
    print json.dumps(j, indent=2, ensure_ascii=False)


def utc_to_local(dt):
    if time.localtime().tm_isdst:
        return dt - timedelta(seconds = time.altzone)
    else:
        return dt - timedelta(seconds = time.timezone)


def local_to_utc(dt):
    if time.localtime().tm_isdst:
        return dt + timedelta(seconds = time.altzone)
    else:
        return dt + timedelta(seconds = time.timezone)


def from_rm_date(str_date):
    return utc_to_local(datetime.strptime(str_date, RM_DATE_FMT))


def date_from_redmine(str_date, to_local=False):
    if to_local:
        return utc_to_local(datetime.strptime(str_date, RM_DATE_FMT))
    else:
        return datetime.strptime(str_date, RM_DATE_FMT)


def date_to_redmine(dt, to_utc=False):
    if to_utc:
        dt = local_to_utc(dt)
    return dt.strftime(RM_DATE_FMT)


def time_diff(date_string, minutes=False):
    td = datetime.now() - utc_to_local(datetime.strptime(date_string, RM_DATE_FMT))
    if minutes:
        return int(td.total_seconds()/60)
    else:
        return delta_to_str(td)


def delta_from_now(**p):
    return datetime.utcnow() - timedelta(**p)


def delta_to_str(td):
    return re.match(r'(.*)\:\d+\.\d+$', str(td)).group(1)


def time_delta_for_sla(sla):
    if sla == '5x8':
        return FILTER_RM_DATE_FMT.format('<=', delta_from_now(hours=8))
    if sla == '24x7':
        return FILTER_RM_DATE_FMT.format('<=', delta_from_now(hours=2))
    return FILTER_RM_DATE_FMT.format('<=', delta_from_now(days=7))


def debug_value(data, key=''):
    if data:
        if data.has_key(key):
            print "DEBUG:", key, '=', data[key]


class requestParams(object):

    def __init__(self, default_params={}):
        super(requestParams, self).__init__()
        self.params = default_params

    def add(self, key, val):
        self.params[key] = val

    def dump(self):
        print json.dumps(self.params, indent=2, ensure_ascii=False)

    def get(self):
        return self.params

    def url(self, base='', url=''):
        return urljoin(base, url + '?' + '&'.join(str('%s=%s' % item) for item in self.params.items()))


class RmClient(object):

    _redmine = None
    _mail = None
    _verbose = False
    _debug = False
    _html = ''
    data_exists = False

    def __init__(self, _conf):
        super(RmClient, self).__init__()
        self._redmine = _conf['redmine']
        self._mail = _conf['mail']
        self._verbose = _conf['verbose']
        self._debug = _conf['debug']
        self._redmine['host'] = urlparse(self._redmine['url']).netloc
        if self._verbose:
            print 'Redmine:', self._redmine['host']

    def _request_url(self, _req='', params=requestParams()):
        if self._debug:
            print 'DEBUG: url:', params.url(self._redmine['url'], _req)
        return params.url(self._redmine['url'], _req)

    def request(self, _req, params=None):
        req = urllib2.Request(url=self._request_url(_req, params))
        req.add_header('X-Redmine-API-Key', self._redmine['api-key'])
        try:
            response = urllib2.urlopen(req).read()
        except urllib2.HTTPError as e:
            print e
            return {}
        except urllib2.URLError as e:
            print e
            return {}
        self._debug_response(response)
        return json.loads(response)

    def put_issue(self, id, data):
        req = urllib2.Request(url=self._request_url('issues/{}.json'.format(id)), data=json.dumps(data))
        req.add_header('X-Redmine-API-Key', self._redmine['api-key'])
        req.add_header('Content-Type', 'application/json')
        req.get_method = lambda: 'PUT'
        try:
            response = urllib2.urlopen(req).read()
        except urllib2.HTTPError as e:
            print "Issue:", id, e, self._request_url('issues/{}.json'.format(id))
            return {}
        except urllib2.URLError as e:
            print "Issue:", id, e, self._request_url('issues/{}.json'.format(id))
            return {}
        if self._verbose:
            print "Issue:", id, 'updated', response

    def _debug_response(self, response):
        if self._debug and response:
        # if response:
            try:
                resp = json.loads(response)
                debug_value(resp, 'total_count')
                debug_value(resp, 'limit')
                debug_value(resp, 'offset')
                # print json.dumps(json.loads(response), indent=2, ensure_ascii=False)
            except ValueError:
                print 'DEBUG:', response

    def html_add(self, value):
        self._html += value

    def issues_new(self):
        self.data_exists = False
        response = self.request('projects.json', requestParams(DEFAULT_PARAMS))
        if response.has_key('projects'):
            for project in response['projects']:
                if project.has_key('custom_fields'):
                    for custom_field in project['custom_fields']:
                        if custom_field['name'] == 'SLA' and custom_field['value']:
                            self._issues(project, custom_field['value'], 1)
            if self._verbose:
                print REPORT_FOOT_FORMAT.format('')

    def _issues(self, project, sla, status):
        params = requestParams(DEFAULT_PARAMS)
        params.add('project_id', project['id'])
        params.add('created_on', time_delta_for_sla(sla))
        params.add('status_id', status)
        DATA_HEAD['project'] = project['name']
        DATA_HEAD['sla'] = sla
        DATA_HEAD['desc'] = SLA[sla]
        DATA_BODY['url'] = self._redmine['url']
        self.html_add(HTML_HEAD_FORMAT.format(**DATA_HEAD))
        if self._verbose:
            print REPORT_HEAD_FORMAT.format(**DATA_HEAD)
        response = self.request('issues.json', params)
        if response.has_key('issues'):
            for issue in response['issues']:
                self.data_exists = True
                DATA_BODY['id'] = issue["id"]
                DATA_BODY['subject'] = issue["subject"]
                DATA_BODY['priority'] = issue["priority"]["name"]
                DATA_BODY['created_on'] = from_rm_date(issue["created_on"])
                DATA_BODY['info'] = time_diff(issue["created_on"])
                self._html += HTML_DATA_FORMAT.format(**DATA_BODY)
                if self._verbose:
                    print REPORT_DATA_FORMAT.format(**DATA_BODY)
        self._html += HTML_FOOT_FORMAT

    def get_new_issues(self, project, sla):
        self._issues(project, sla, 1)

    def issues_without_due_date(self):
        self._mail['subject'] = MAIL_SUBJECT['duedate']
        DATA_HEAD['delta'] = 'Project'
        DATA_BODY['url'] = self._redmine['url']
        self._html += HTML_HEAD_FORMAT2.format(**DATA_HEAD)

        params = requestParams(DEFAULT_PARAMS)
        params.add('status', 'open')
        response = self.request('issues.json', params)
        if response.has_key('issues'):
            if self._verbose:
                print REPORT_HEAD_FORMAT.format(**DATA_HEAD)
            for issue in response['issues']:
                if not issue.has_key('due_date'):
                    self.data_exists = True
                    DATA_BODY['id'] = issue["id"]
                    DATA_BODY['subject'] = issue["subject"]
                    DATA_BODY['priority'] = issue["priority"]["name"]
                    DATA_BODY['created_on'] = from_rm_date(issue["created_on"])
                    DATA_BODY['info'] = issue["project"]["name"]
                    self._html += HTML_DATA_FORMAT.format(**DATA_BODY)
                    if self._verbose:
                        print REPORT_DATA_FORMAT.format(**DATA_BODY)
            self._html += HTML_FOOT_FORMAT

    def fix_due_date(self):
        params = requestParams(DEFAULT_PARAMS)
        params.add('status_id', 'open')
        response = self.request('issues.json', params)
        if response.has_key('issues'):
            for issue in response['issues']:
                if not issue.has_key('due_date'):
                    due_date = date_from_redmine(issue["created_on"]) + timedelta(days=14)
                    self.put_issue(issue["id"], {"issue": {"due_date": due_date.strftime("%Y-%m-%d")}})

    def send_mail(self):
        if self._debug:
            print HTML_FORMAT.format(DATA=self._html.encode('utf-8'))
        if self.data_exists:
            html = HTML_FORMAT.format(DATA=self._html.encode('utf-8'))
            msg = MIMEMultipart('alternative', None, [MIMEText(html, 'html', 'utf-8')])
            msg['Subject'] = self._mail['subject']
            msg['From'] = self._mail['from']
            msg['To'] = self._mail['to']
            server=None
            try:
                server = smtplib.SMTP_SSL(self._mail['host'],self._mail['port'])
                if self._debug:
                    server.set_debuglevel(1)
                server.login(self._mail['user'], self._mail['password'])
                server.sendmail(self._mail['from'], self._mail['to'].split(','), msg.as_string())
                server.quit()
            except smtplib.SMTPAuthenticationError as e:
                print 'Send mail [SMTPAuthenticationError]:', e.smtp_code, e.smtp_error
                server.quit()
            except smtplib.SMTPRecipientsRefused as e:
                print 'Send mail [SMTPRecipientsRefused]:'
                for (k, v) in e.recipients.items():
                    print k, v[0], v[1]
                server.quit()
            except Exception as e:
                print 'Send mail error:', e


def load_config(fname):
    if os.path.isfile(fname):
        config = ConfigParser.ConfigParser(allow_no_value=True)
        try:
            config.readfp(open(fname))
            for section in config.sections():
                for (name, value) in config.items(section):
                    if not DEFAULT_CONFIG.get(section): DEFAULT_CONFIG[section]={}
                    DEFAULT_CONFIG[section][name] = value.strip("'\"")
        except ConfigParser.MissingSectionHeaderError as e:
            print e
        except Exception as e:
            print e


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=PROG_DESC)
    parser.add_argument('-c', '--config', default='/etc/'+ PROG +'.conf')
    parser.add_argument('-f', '--fix', action='store_true', help="Fix the due date")
    parser.add_argument('-n', '--new', action='store_true', help="New issues")
    parser.add_argument('-w', '--wdd', action='store_true', help="Issues without due date")
    parser.add_argument('-s', '--send', action='store_true', help="Send notifications")
    parser.add_argument('-d', '--debug', action='store_true', help="Debug output")
    parser.add_argument('-v', '--verbose', action='store_true', help="Verbose output")
    args = parser.parse_args()

    if args.config: load_config(args.config)

    DEFAULT_CONFIG['verbose'] = args.verbose
    DEFAULT_CONFIG['debug'] = args.debug

    # if args.debug:
    #     print 'CONFIG:', json.dumps(DEFAULT_CONFIG, indent=2)

    rm = RmClient(DEFAULT_CONFIG)
    if args.new:
        rm.issues_new()
    elif args.wdd:
        rm.issues_without_due_date()
    elif args.fix:
        rm.fix_due_date()
    else:
        parser.print_help()
    if args.send:
        rm.send_mail()

