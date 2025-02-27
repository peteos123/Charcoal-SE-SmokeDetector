# coding=utf-8
import os
import sys
import traceback
from datetime import datetime
import importlib
import threading
# termcolor doesn't work properly in PowerShell or cmd on Windows, so use colorama.
import platform
platform_text = platform.platform().lower()
if 'windows' in platform_text and 'cygwin' not in platform_text:
    from colorama import init as colorama_init
    colorama_init()
from termcolor import colored
import requests
import regex
from glob import glob
import sqlite3
from urllib.parse import quote, quote_plus
from globalvars import GlobalVars
from threading import Thread


def exit_mode(*args, code=0):
    args = set(args)

    if not (args & {'standby', 'no_standby'}):
        standby = 'standby' if GlobalVars.standby_mode else 'no_standby'
        args.add(standby)

    with open("exit.txt", "w", encoding="utf-8") as f:
        print("\n".join(args), file=f)
    log('debug', 'Exiting with args: {}'.format(', '.join(args) or 'None'))

    # Flush any buffered queue timing data
    import datahandling  # this must not be a top-level import in order to avoid a circular import
    datahandling.flush_queue_timings_data()
    datahandling.store_recently_scanned_posts()

    # We have to use '_exit' here, because 'sys.exit' only exits the current
    # thread (not the current process).  Unfortunately, this results in
    # 'atexit' handlers not being called. All exit calls in SmokeDetector go
    # through this function, so any necessary cleanup can happen here (though
    # keep in mind that this function isn't called when terminating due to a
    # Ctrl-C or other signal).
    os._exit(code)


class ErrorLogs:
    DB_FILE = "errorLogs.db"
    # SQLite threading limitation !?!?!?

    db = sqlite3.connect(DB_FILE)
    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='error_logs'").fetchone() is None:
        # Table 'error_logs' doesn't exist
        try:
            db.execute("CREATE TABLE error_logs (time REAL PRIMARY KEY ASC, classname TEXT, message TEXT,"
                       " traceback TEXT)")
            db.commit()
        except (sqlite3.OperationalError):
            # In CI testing, it's possible for the table to be created in a different thread between when
            # we first test for the table's existanceand when we try to create the table.
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='error_logs'").fetchone() is None:
                # Table 'error_logs' still doesn't exist
                raise
    db.close()

    db_conns = {}

    @classmethod
    def get_db(cls):
        thread_id = threading.get_ident()
        if thread_id not in cls.db_conns:
            cls.db_conns[thread_id] = sqlite3.connect(cls.DB_FILE)
        return cls.db_conns[thread_id]

    @classmethod
    def add(cls, time, classname, message, traceback):
        classname = redact_passwords(classname)
        message = redact_passwords(message)
        traceback = redact_passwords(traceback)
        db = cls.get_db()
        db.execute("INSERT INTO error_logs VALUES (?, ?, ?, ?)",
                   (time, classname, message, traceback))
        db.commit()

    @classmethod
    def fetch_last(cls, n):
        db = cls.get_db()
        cursor = db.execute("SELECT * FROM error_logs ORDER BY time DESC LIMIT ?", (int(n),))
        data = cursor.fetchall()
        return data

    @classmethod
    def truncate(cls, n=100):
        """
        Truncate the DB and keep only N latest exceptions
        """
        db = cls.get_db()
        cursor = db.execute(
            "DELETE FROM error_logs WHERE time IN "
            "(SELECT time FROM error_logs ORDER BY time DESC LIMIT -1 OFFSET ?)", (int(n),))
        db.commit()
        data = cursor.fetchall()
        return data


class Helpers:
    min_log_level = 0


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


def redact_text(text, redact_str, replace_with):
    if redact_str:
        return text.replace(redact_str, replace_with) \
                   .replace(quote(redact_str), replace_with) \
                   .replace(quote_plus(redact_str), replace_with)
    return text


def redact_passwords(value):
    value = str(value)
    # Generic redaction of URLs with http, https, and ftp schemes
    value = regex.sub(r"((?:https?|ftp):\/\/)[^@:\/]*:[^@:\/]*(?=@)", r"\1[REDACTED URL USERNAME AND PASSWORD]", value)
    # In case these are somewhere else.
    value = redact_text(value, GlobalVars.github_password, "[GITHUB PASSWORD REDACTED]")
    value = redact_text(value, GlobalVars.github_access_token, "[GITHUB ACCESS TOKEN REDACTED]")
    value = redact_text(value, GlobalVars.chatexchange_p, "[CHAT PASSWORD REDACTED]")
    value = redact_text(value, GlobalVars.metasmoke_key, "[METASMOKE KEY REDACTED]")
    value = redact_text(value, GlobalVars.perspective_key, "[PERSPECTIVE KEY REDACTED]")
    return value


# noinspection PyMissingTypeHints
def log(log_level, *args, f=False):
    levels = {
        'debug': [0, 'grey'],
        'info': [1, 'cyan'],
        'warning': [2, 'yellow'],
        'warn': [2, 'yellow'],
        'error': [3, 'red']
    }

    level = levels[log_level][0]
    if level < Helpers.min_log_level:
        return

    color = levels[log_level][1] if log_level in levels else 'white'
    log_str = "{} {}".format(colored("[{}]".format(datetime.utcnow().isoformat()[11:-3]),
                                     color, attrs=['bold']),
                             redact_passwords("  ".join([str(x) for x in args])))
    print(log_str, file=sys.stderr)

    if level == 3:
        exc_tb = sys.exc_info()[2]
        print(redact_passwords("".join(traceback.format_tb(exc_tb))), file=sys.stderr)

    if f:  # Also to file
        log_file(log_level, *args)


def log_file(log_level, *args):
    levels = {
        'debug': 0,
        'info': 1,
        'warning': 2,
        'error': 3,
    }
    if levels[log_level] < Helpers.min_log_level:
        return

    log_str = redact_passwords("[{}] {}: {}".format(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                                                    log_level.upper(), "  ".join([str(x) for x in args])))
    with open("errorLogs.txt", "a", encoding="utf-8") as f:
        print(log_str, file=f)


def log_exception(exctype, value, traceback_or_message, f=False, *, level='error'):
    now = datetime.utcnow()
    if isinstance(traceback_or_message, str):
        tr = traceback_or_message
    else:
        tr = ''.join(traceback.format_tb(traceback_or_message))
    exception_only = ''.join(traceback.format_exception_only(exctype, value)).strip()
    logged_msg = "{exception}\n{now} UTC\n{row}\n\n".format(exception=exception_only, now=now, row=tr)
    # Redacting passwords happens in log() and ErrorLogs.add().
    log(level, logged_msg, f=f)
    ErrorLogs.add(now.timestamp(), exctype.__name__, str(value), tr)


def log_current_exception(f=False):
    log_exception(*sys.exc_info(), f)


def files_changed(diff, file_set):
    changed = set(diff.split())
    return bool(len(changed & file_set))


core_files = {
    "apigetpost.py", "blacklists.py", "bodyfetcher.py", "chatcommands.py", "chatcommunicate.py",
    "chatexchange_extension.py", "datahandling.py", "deletionwatcher.py", "excepthook.py", "flovis.py",
    "gitmanager.py", "globalvars.py", "helpers.py", "metasmoke.py", "nocrash.py", "parsing.py",
    "spamhandling.py", "socketscience.py", "tasks.py", "ws.py",

    "classes/feedback.py", "_Git_Windows.py", "classes/__init__.py", "classes/_Post.py",

    # Before these are made reloadable
    "rooms.yml",
}
reloadable_modules = {
    "findspam.py",
}
module_files = core_files | reloadable_modules


def only_blacklists_changed(diff):
    return not files_changed(diff, module_files)


def only_modules_changed(diff):
    return not files_changed(diff, core_files)


def reload_modules():
    result = True
    for s in reloadable_modules:
        s = s.replace(".py", "")  # Relying on our naming convention
        try:
            # Some reliable approach
            importlib.reload(sys.modules[s])
        except (KeyError, ImportError):
            result = False
    return result


def unshorten_link(url, request_type='GET', depth=10):
    orig_url = url
    response_code = 301
    headers = {'User-Agent': 'SmokeDetector/git (+https://github.com/Charcoal-SE/SmokeDetector)'}
    for tries in range(depth):
        if response_code not in {301, 302, 303, 307, 308}:
            break
        res = requests.request(request_type, url, headers=headers, stream=True, allow_redirects=False)
        res.connection.close()  # Discard response body for GET requests
        response_code = res.status_code
        if 'Location' not in res.headers:
            # No more redirects, stop
            break
        url = res.headers['Location']
    else:
        raise ValueError("Too many redirects ({}) for URL {!r}".format(depth, orig_url))
    return url


pcre_comment = regex.compile(r"\(\?#(?<!(?:[^\\]|^)(?:\\\\)*\\\(\?#)[^)]*\)")


def blacklist_integrity_check():
    bl_files = glob('bad_*.txt') + glob('blacklisted_*.txt') + glob('watched_*.txt')
    seen = dict()
    errors = []
    city_list = ['test']
    regex.cache_all(False)
    for bl_file in bl_files:
        with open(bl_file, 'r', encoding="utf-8") as lines:
            for lineno, line in enumerate(lines, 1):
                if line.endswith('\r\n'):
                    errors.append('{0}:{1}:DOS line ending'.format(bl_file, lineno))
                elif not line.endswith('\n'):
                    errors.append('{0}:{1}:No newline'.format(bl_file, lineno))
                elif line == '\n':
                    errors.append('{0}:{1}:Empty line'.format(bl_file, lineno))
                elif bl_file.startswith('watched_'):
                    line = line.split('\t')[2]
                if 'numbers' not in bl_file:
                    try:
                        regex.compile(line, regex.UNICODE, city=city_list, ignore_unused=True)
                    except Exception:
                        (exctype, value, traceback_or_message) = sys.exc_info()
                        exception_only = ''.join(traceback.format_exception_only(exctype, value)).strip()
                        errors.append("{0}:{1}:Regex fails to compile:r'''{2}''':{3}".format(bl_file, lineno,
                                                                                             line.rstrip('\n'),
                                                                                             exception_only))
                line = pcre_comment.sub("", line)
                if line in seen:
                    errors.append('{0}:{1}:Duplicate entry {2} (also {3})'.format(
                        bl_file, lineno, line.rstrip('\n'), seen[line]))
                else:
                    seen[line] = '{0}:{1}'.format(bl_file, lineno)
    regex.cache_all(True)
    return errors


def chunk_list(list_in, chunk_size):
    """
    Split a list into chunks.
    """
    return [list_in[i:i + chunk_size] for i in range(0, len(list_in), chunk_size)]


class SecurityError(Exception):
    pass


def not_regex_search_ascii_and_unicode(regex_dict, test_text):
    return not regex_dict['ascii'].search(test_text) and not regex_dict['unicode'].search(test_text)


def remove_regex_comments(regex_text):
    return regex.sub(r"(?<!\\)\(\?\#[^\)]*\)", "", regex_text)


def remove_end_regex_comments(regex_text):
    return regex.sub(r"(?:(?<!\\)\(\?\#[^\)]*\))+$", "", regex_text)


def get_only_digits(text):
    return regex.sub(r"(?a)\D", "", text)


def add_to_global_bodyfetcher_queue_in_new_thread(hostname, question_id, should_check_site=False):
    t = Thread(name="bodyfetcher post enqueuing: {}/{}".format(hostname, question_id),
               target=GlobalVars.bodyfetcher.add_to_queue,
               args=(hostname, question_id, should_check_site))
    t.start()


def get_recently_scanned_key_for_post(post):
    site = post.get('site', None)
    post_id = post.get('question_id', None)
    if post_id is None:
        post_id = post.get('answer_id', None)
    if site is None or post_id is None:
        log('warn', 'Unable to determine site or post_id for add_recently_scanned_post:'
                    ' site:{}:: post_id: {}'.format(site, post_id))
        return
    return "{}/{}".format(site, post_id)


def get_check_equality_data(post):
    owner_dict = post.get('owner', {})
    owner_name = owner_dict.get('display_name', None)
    return (
        post.get('last_edit_date', None),
        post.get('title', None),
        owner_name,
        post.get('body', None),
    )


def is_post_recently_scanned_and_unchanged(post):
    post_key = get_recently_scanned_key_for_post(post)
    with GlobalVars.recently_scanned_posts_lock:
        scanned_post = GlobalVars.recently_scanned_posts.get(post_key, None)
    if scanned_post is None:
        return False
    post_equality_data = get_check_equality_data(post)
    scanned_equality_data = get_check_equality_data(scanned_post)
    is_unchanged = post_equality_data == scanned_equality_data
    if not is_unchanged and post_equality_data[0] == scanned_equality_data[0]:
        # This should be a grace period edit
        results = [post_equality_data[count] == scanned_equality_data[count]
                   for count in range(0, len(post_equality_data))]
        log('debug', 'GRACE period edit: {}:: results:{}'.format(post_key, results))
    return is_unchanged
