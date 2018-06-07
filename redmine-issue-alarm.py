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

P_ISSUES = {'sort': 'id', 'status_id': 1}
RM_DATE_FMT = '%Y-%m-%dT%H:%M:%SZ'
FILTER_RM_DATE_FMT = '{}{:%Y-%m-%dT%H:%M:%SZ}'

DATA_HEAD = {
    'head': '',
    'project': '',
    'sla': 'No',
    'desc': '',
    'id': "id",
    'subject': "Subject",
    'priority': "Priority",
    'created_on': "Created",
    'delta': "Expired"
}

DATA_BODY = {
    'url': '',
    'id': 0,
    'subject': '',
    'priority': '',
    'created_on': '',
    'delta':  ''
}


REPORT_HEAD_FORMAT=u'''{head:=^80}
{project:^80}
{sla:^80}
{desc:-^80}
{id:>5s} | {subject:32.32s} | {priority:6.6s} | {created_on:11s} | {delta}'''
REPORT_DATA_FORMAT=u'{id:5d} | {subject:32.32s} | {priority:6.6s} | {created_on:%d/%m %H:%M} | {delta}'
REPORT_FOOT_FORMAT=u'{:=^80}'

HTML_HEAD_FORMAT=u'''<P>
<UL>
<LI>Project: {project}</LI>
<LI>SLA: {sla}</LI>
<LI>{desc}</LI>
</UL>
<TABLE>
<TR><TH>{subject:s}</TH><TH>{priority:s}</TH><TH>{created_on:s}</TH><TH>{delta}</TH></TR>
'''
HTML_DATA_FORMAT=u"""<TR><TD><A href='{url}/issues/{id:d}'><B>#{id:d}</B></A> {subject:s}</TD><TD>{priority:s}</TD><TD>{created_on:%Y-%m-%d %H:%M}</TD><TD>{delta}</TD></TR>\n"""
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


def _filter(_request, params={}):
    _request += '?limit=100'
    for (k, v) in params.items():
        _request += '&%s=%s' % (k, v)
    return _request


def utc_to_local(dt):
    if time.localtime().tm_isdst:
        return dt - timedelta(seconds = time.altzone)
    else:
        return dt - timedelta(seconds = time.timezone)


def from_rm_date(str_date):
    return utc_to_local(datetime.strptime(str_date, RM_DATE_FMT))


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


def get_sla_delta(sla):
    if sla == '5x8':
        return FILTER_RM_DATE_FMT.format('<=', delta_from_now(hours=8))
    if sla == '24x7':
        return FILTER_RM_DATE_FMT.format('<=', delta_from_now(hours=2))
    return FILTER_RM_DATE_FMT.format('<=', delta_from_now(days=7))


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

    def _request_url(self, _req=''):
        if self._debug:
            print 'DEBUG: url: %s/%s' % (self._redmine['url'], _req)
        return '%s/%s' % (self._redmine['url'], _req)

    def _request(self, _req):
        req = urllib2.Request(url=self._request_url(_req))
        req.add_header('X-Redmine-API-Key', self._redmine['api-key'])
        try:
            response = urllib2.urlopen(req).read()
        except urllib2.HTTPError as e:
            print e
            return {}
        except urllib2.URLError as e:
            print e
            return {}
        if self._debug:
            print json.dumps(json.loads(response), indent=2, ensure_ascii=False)
        return json.loads(response)

    def request(self, _req, params={}):
        return self._request(_filter(_req, params))

    def get_projects(self):
        self.data_exists = False
        response = self.request('projects.json')
        if response.has_key('projects'):
            for project in response['projects']:
                if project.has_key('custom_fields'):
                    for custom_field in project['custom_fields']:
                        if custom_field['value']:
                            self.get_new_issues(project, custom_field['value'])
            if self._verbose:
                print REPORT_FOOT_FORMAT.format('')

    def _issues(self, project, sla, status):
        P_ISSUES['project_id'] = project['id']
        P_ISSUES['created_on'] = get_sla_delta(sla)
        P_ISSUES['status_id'] = status
        DATA_HEAD['project'] = project['name']
        DATA_HEAD['sla'] = sla
        DATA_HEAD['desc'] = SLA[sla]
        DATA_BODY['url'] = self._redmine['url']
        self._html += HTML_HEAD_FORMAT.format(**DATA_HEAD)
        if self._verbose:
            print REPORT_HEAD_FORMAT.format(**DATA_HEAD)
        response = self.request('issues.json', P_ISSUES)
        if response.has_key('issues'):
            for issue in response['issues']:
                self.data_exists = True
                DATA_BODY['id'] = issue["id"]
                DATA_BODY['subject'] = issue["subject"]
                DATA_BODY['priority'] = issue["priority"]["name"]
                DATA_BODY['created_on'] = from_rm_date(issue["created_on"])
                DATA_BODY['delta'] = time_diff(issue["created_on"])
                self._html += HTML_DATA_FORMAT.format(**DATA_BODY)
                if self._verbose:
                    print REPORT_DATA_FORMAT.format(**DATA_BODY)
        self._html += HTML_FOOT_FORMAT

    def get_new_issues(self, project, sla):
        self._issues(project, sla, 1)

    def send_mail(self):
        if self._debug:
            print HTML_FORMAT.format(DATA=self._html.encode('utf-8'))
        if self.data_exists:
            html = HTML_FORMAT.format(DATA=self._html.encode('utf-8'))
            msg = MIMEMultipart('alternative', None, [MIMEText(html, 'html','utf-8')])
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
    parser.add_argument('-s', '--send', action='store_true', help="Send notifications")
    parser.add_argument('-d', '--debug', action='store_true', help="Debug output")
    parser.add_argument('-v', '--verbose', action='store_true', help="Verbose output")
    args = parser.parse_args()

    if args.config: load_config(args.config)

    DEFAULT_CONFIG['verbose'] = args.verbose
    DEFAULT_CONFIG['debug'] = args.debug

    if args.debug:
        print 'CONFIG:', json.dumps(DEFAULT_CONFIG, indent=2)

    rm = RmClient(DEFAULT_CONFIG)
    if args.fix or args.send:
        rm = RmClient(DEFAULT_CONFIG)
        rm.get_projects()
        rm.send_mail()
    else:
        if args.verbose:
            rm.get_projects()
        else:
            parser.print_help()
