# coding=utf-8
import os
import sys
from datetime import datetime
from termcolor import colored
import requests
import regex
from glob import glob


class Helpers:
    min_log_level = 0


# Allows use of `environ_or_none("foo") or "default"` shorthand
# noinspection PyBroadException,PyMissingTypeHints
def environ_or_none(key):
    try:
        return os.environ[key]
    except KeyError:
        return None


def escape_format(s):
    return s.replace("{", "{{").replace("}", "}}")


def expand_shorthand_link(s):
    s = s.lower()
    if s.endswith("so"):
        s = s[:-2] + "stackoverflow.com"
    elif s.endswith("se"):
        s = s[:-2] + "stackexchange.com"
    elif s.endswith("su"):
        s = s[:-2] + "superuser.com"
    elif s.endswith("sf"):
        s = s[:-2] + "serverfault.com"
    elif s.endswith("au"):
        s = s[:-2] + "askubuntu.com"
    return s


# noinspection PyMissingTypeHints
def log(log_level, *args):
    levels = {
        'debug': [0, 'grey'],
        'info': [1, 'cyan'],
        'warning': [2, 'yellow'],
        'error': [3, 'red']
    }

    level = levels[log_level][0]
    if level < Helpers.min_log_level:
        return

    color = (levels[log_level][1] if log_level in levels else 'white')
    log_str = u"{} {}".format(colored("[{}]".format(datetime.now().isoformat()[11:-7]), color),
                              u"  ".join([str(x) for x in args]))
    print(log_str)


def only_files_changed(diff, file_set):
    files_changed = set(diff.split())
    return len(files_changed - file_set) == 0


no_reboot_files = {
    "",  # In case an empty string comes out of str.split()
    "bad_keywords.txt", "blacklisted_usernames.txt", "blacklisted_websites.txt", "watched_keywords.txt",
}
no_reboot_modules = no_reboot_files.union({
    "findspam.py",
})


def only_blacklists_changed(diff):
    return only_files_changed(diff, no_reboot_files)


def only_module_changed(diff):
    return only_files_changed(diff, no_reboot_modules)


# FAIR WARNING: Sending HEAD requests to resolve a shortened link is generally okay - there aren't
# as many exploits that work on just HEAD responses. If you specify sending a GET request, you
# acknowledge that this will fetch the full, potentially unsafe response from the shortener.
def unshorten_link(url, request_type='HEAD', explicitly_ignore_security_warning=False):
    requesters = {
        'GET': requests.get,
        'HEAD': requests.head
    }
    if request_type not in requesters:
        raise KeyError('Unavailable request_type {}'.format(request_type))
    if request_type == 'GET' and not explicitly_ignore_security_warning:
        raise SecurityError('Potentially unsafe request type GET not acknowledged')

    requester = requesters[request_type]
    response_code = 301
    headers = {'User-Agent': 'SmokeDetector/git (+https://github.com/Charcoal-SE/SmokeDetector)'}
    while response_code in [301, 302, 303, 307, 308]:
        res = requester(url, headers=headers)
        response_code = res.status_code
        if 'Location' in res.headers:
            url = res.headers['Location']

    return url


parser_regex = r'((?:meta\.)?(?:(?:(?:math|(?:\w{2}\.)?stack)overflow|askubuntu|superuser|serverfault)|\w+)' \
               r'(?:\.meta)?)\.(?:stackexchange\.com|com|net)'
parser = regex.compile(parser_regex)
exceptions = {
    'meta.superuser': 'meta.superuser',
    'meta.serverfault': 'meta.serverfault',
    'meta.askubuntu': 'meta.askubuntu',
    'mathoverflow': 'mathoverflow.net',
    'meta.mathoverflow': 'meta.mathoverflow.net',
    'meta.stackexchange': 'meta'
}


def api_parameter_from_link(link):
    match = parser.search(link)
    if match:
        if match[1] in exceptions.keys():
            return exceptions[match[1]]
        elif 'meta.' in match[1] and 'stackoverflow' not in match[1]:
            return '.'.join(match[1].split('.')[::-1])
        else:
            return match[1]
    else:
        return None


id_parser_regex = r'(?:https?:)?//[^/]+/\w+/(\d+)'
id_parser = regex.compile(id_parser_regex)


def post_id_from_link(link):
    match = id_parser.search(link)
    if match:
        return match[1]
    else:
        return None


def to_metasmoke_link(post_url, protocol=True):
    return "{}//m.erwaysoftware.com/posts/uid/{}/{}".format(
        "https:" if protocol else "", api_parameter_from_link(post_url), post_id_from_link(post_url))


def blacklist_integrity_check():
    bl_files = glob('bad_*.txt') + glob('blacklisted_*.txt') + ['watched_keywords.txt']
    seen = dict()
    errors = []
    for bl_file in bl_files:
        with open(bl_file, 'r') as lines:
            for lineno, line in enumerate(lines, 1):
                if line.endswith('\r\n'):
                    errors.append('{0}:{1}:DOS line ending'.format(bl_file, lineno))
                elif not line.endswith('\n'):
                    errors.append('{0}:{1}:No newline'.format(bl_file, lineno))
                elif line == '\n':
                    errors.append('{0}:{1}:Empty line'.format(bl_file, lineno))
                elif bl_file == 'watched_keywords.txt':
                    line = line.split('\t')[2]

                if line in seen:
                    errors.append('{0}:{1}:Duplicate entry {2} (also {3})'.format(
                        bl_file, lineno, line.rstrip('\n'), seen[line]))
                else:
                    seen[line] = '{0}:{1}'.format(bl_file, lineno)
    return errors


class SecurityError(Exception):
    pass
