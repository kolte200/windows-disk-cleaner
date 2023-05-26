#!/usr/bin/python3

# Disk cleaner

"""
Sources:
https://www.technipages.com/files-folders-you-can-safely-delete-in-windows-10/
https://support.microsoft.com/en-us/windows/tips-to-free-up-drive-space-on-your-pc-4d97fc4a-0175-8d49-ac2f-bcf27de46d34
https://github.com/builtbybel/CleanmgrPlus/tree/master/scripts
https://learn.microsoft.com/en-us/microsoftteams/troubleshoot/teams-administration/clear-teams-cache
https://learn.microsoft.com/en-us/troubleshoot/windows-client/shell-experience/larger-windowsdotedb-file
"""

# pip cache remove *
# mvn dependency:purge-local-repository
# npm cache clean
# ccleaner.exe /AUTO
# %TMP% : Delete old files (not all because some are important)
# Delete incomplets downloads files
# Delete : %AppData%/Microsoft/Teams/???
# Delete : %LocalAppData%/Unity/cache
# Delete : %LocalAppData%/Microsoft/vscode-cpptools/ipch
# Delete old files in : %AppData%/Code/User/workspaceStorage
# Delete : %AppData%/Code/{Cache,CachedDate,CachedExtensions,Code Cache}
# Delete old files in : %UserProfile%/.cache
# Delete : %UserProfile%/.gradle/caches/
# Delete : %LocalAppData%/UnrealEngine/Common/DerivedDataCache
# cleanmgr.exe /sageset:1
# cleanmgr.exe /sagerun:1
# Defragment system drive : defrag.exe c:
# Defragment all : defrag.exe /c


import winreg
import os
import re
import json
from ctypes import windll
from send2trash import send2trash
import shutil
import subprocess
import time


hkeys = {}


hkey_id_by_name = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE
}


def reg_get_hkey(name: str) -> winreg.HKEYType | None:
    name = name.upper()
    if name not in hkey_id_by_name: return None
    hkey_id = hkey_id_by_name[name]
    if hkey_id not in hkeys or hkeys[hkey_id] is None:
        hkeys[hkey_id] = winreg.ConnectRegistry(None, hkey_id)
    return hkeys[hkey_id]


def read_reg_key(path: str):
    parts = path.replace('\\', '/').split('/')
    hkey_name = parts[0]
    hkey = reg_get_hkey(hkey_name)
    if hkey is None: return None, -1
    location = '\\'.join(parts[1:-1])
    key = winreg.OpenKey(hkey, location)
    try:
        if len(parts[-1]) > 0:
            value, type = winreg.QueryValueEx(key, parts[-1])
        else:
            value, type = winreg.QueryValue(key, None), winreg.REG_SZ
    except:
        return None, -1
    return value, type


def read_reg_str(path: str) -> str | None:
    v, t = read_reg_key(path)
    if t == winreg.REG_SZ: return v
    return None


def path_part_to_re_escape(c: str) -> str:
    if c in ['.', '{', '}', '[', ']', '/', '\\', '|', '(', ')', '$', '^', '?', '*']:
        return "\\" + c
    return c


def path_part_to_re(part: str) -> tuple[str,list[str]]:
    resolveds = [""]
    new_resolveds = []
    regex = ""
    ctx = 0
    chrs = ""
    for c in part:
        if ctx == 0:
            if c == '*':
                regex += "[^\\/\\\\]*"
                resolveds = []
            elif c == '[':
                ctx = 1
                chrs = ""
                new_resolveds = []
            else:
                regex += path_part_to_re_escape(c)
                for i in range(len(resolveds)):
                    resolveds[i] += c
        elif ctx == 1:
            if c == ']':
                ctx = 0
                regex += "[%s]" % chrs
                resolveds = new_resolveds
            else:
                chrs += path_part_to_re_escape(c)
                for i in range(len(resolveds)):
                    new_resolveds.append(resolveds[i] + c)
    return regex, resolveds


def normalize_path_parts(parts: list[str]) -> list[str]:
    parts_normalized = []
    for part in parts:
        if len(part) == 0 or part == ".":
            continue
        if part == ".." and len(parts_normalized) > 0:
            parts_normalized.pop()
            continue
        parts_normalized.append(part)
    return parts_normalized


def get_drives() -> list[str]:
    drives = []
    bitmask = windll.kernel32.GetLogicalDrives()
    for letter in range(ord('A'), ord('Z') + 1):
        if bitmask & 1:
            drives.append(chr(letter))
        bitmask >>= 1
    return drives


MAX_RESOLVEDS = 16


class PathPattern:
    def __init__(self) -> None:
        self.paths: list[list[str]] = [] # Regex paths
        self.resolveds: list[str] = [] # Exacts paths (empty when not possible)
        self.compiled: re.Pattern | None = None
        self.limited = True
        self.modified = True

    def union(self, other: 'PathPattern') -> None:
        self.paths.extend(other.paths)
        if self.limited and other.limited:
            self.resolveds.extend(other.resolveds)
        self.modified = True

    def add(self, path: str) -> None:
        if len(path) == 0: return

        parts = path.replace('\\', '/').split('/')
        add_resolveds = [""]
        regex_parts = []
        for regex_part, resolveds_part in map(path_part_to_re, normalize_path_parts(parts)):
            regex_parts.append(regex_part)
            new_resolved = []
            for i in range(len(add_resolveds)):
                base = add_resolveds[i]
                if len(base) > 0: base += '/'
                for resolved in resolveds_part:
                    new_resolved.append(base + resolved)
            add_resolveds = new_resolved

        self.paths.append(regex_parts)

        if len(add_resolveds) == 0:
            self.resolveds.clear()
            self.limited = False
        else:
            self.resolveds.extend(add_resolveds)

        self.modified = True

    def compile(self) -> None:
        if not self.modified: return

        partial_pattern = ""
        full_pattern = ""

        for parts in self.paths:
            new_partial_pattern = parts[-1]
            for i in range(len(parts)-2, -1, -1):
                new_partial_pattern = "%s/?(/%s)?" % (parts[i], new_partial_pattern)

            if len(partial_pattern) > 0: partial_pattern += '|'
            partial_pattern += "(%s)" % new_partial_pattern

            new_full_pattern = '/'.join(parts)

            if len(full_pattern) > 0: full_pattern += '|'
            full_pattern += "(%s)" % new_full_pattern

        self.compiled = re.compile("(%s)|(%s)" % (full_pattern, partial_pattern), re.IGNORECASE)

        self.modified = False

    def join(self, other: 'PathPattern') -> None:
        self.limited = self.limited and other.limited
        if self.limited:
            spaths = self.resolveds
            self.resolveds = []
            for opath in other.resolveds:
                for spath in spaths:
                    self.resolveds.append(spath + '/' + opath)
        else:
            self.resolveds.clear()

        spaths = self.paths
        self.paths = []
        for opath in other.paths:
            for spath in spaths:
                self.paths.append(normalize_path_parts(spath + opath))

        self.modified = True

    def search_rec(self, path: str, one: bool, childs: bool, founds: list[str]) -> bool:
        if self.compiled is None: return False

        match = self.compiled.fullmatch(path)
        if match is None: return False

        r = False
        if match.group(1) is not None:
            founds.append(path)
            r = True
            if one or not childs: return True

        if not os.path.isdir(path): return r
        for file in os.listdir(path):
            if not path.endswith('/'): file = path + '/' + file
            else: file = path + file
            r = self.search_rec(file, one, childs, founds) or r
            if r and one: return True

        return r

    def search(self, one = False, childs = True) -> list[str]:
        founds = []

        self.compile()
        if self.compiled is None: return []

        if self.limited and len(self.resolveds) <= MAX_RESOLVEDS:
            for path in self.resolveds:
                if os.path.exists(path):
                    founds.append(path)
                    if one: break

        else:
            drives = get_drives()
            for drive in drives:
                if self.search_rec(drive + ":/", one, childs, founds) and one: break

        return founds


def conf_get_paths_env(path):
    return os.path.expandvars(path)


def conf_get_paths_rec(conf, p: PathPattern) -> None:
    if isinstance(conf, str):
        p.add(conf_get_paths_env(conf))

    elif isinstance(conf, list):
        for e in conf:
            conf_get_paths_rec(e, p)

    elif "reg" in conf:
        v = conf["reg"]
        regs = v if isinstance(v, list) else [v]
        for reg in regs:
            s = read_reg_str(reg)
            if s is not None: p.add(conf_get_paths_env(s))

    elif "join" in conf:
        v = conf["join"]
        parts = []
        for e in v:
            parts.append(PathPattern())
            conf_get_paths_rec(e, parts[-1])
        for i in range(1, len(parts)):
            parts[0].join(parts[i])
        if len(parts) > 0: p.union(parts[0])


def conf_get_paths(conf) -> PathPattern:
    p = PathPattern()
    conf_get_paths_rec(conf, p)
    return p


def conf_get_duration(conf) -> int:
    if isinstance(conf, str):
        code = conf[-1]
        val = int(conf[:-1])
        if code == 'd':
            return val * 24 * 60 * 60 * 1000
        if code == 'h':
            return val * 60 * 60 * 1000
        if code == 'm':
            return val * 60 * 1000
        if code == 's':
            return val * 1000

    elif isinstance(conf, int) or isinstance(conf, float):
        return int(conf)

    return 0


MAX_AGE = 0xFFFFFFFFFFFFFFFF


def run_rule(rule):
    if "includes" in rule:
        includes = conf_get_paths(rule["includes"]).search()
        for include in includes:
            before = shutil.disk_usage("/").used
            rules = []
            with open(include, "r", encoding="utf8") as file:
                rules = json.load(file)
            for rule in rules:
                run_rule(rule)
            after = shutil.disk_usage("/").used
            print("Include '%s' and %.3f MB has been freed" % (include, (before - after) / (1024*1024)))

    elif "rm" in rule:
        rm = rule["rm"]

        min_age = 0
        max_age = MAX_AGE
        if "age" in rm:
            d = rm["age"]
            if "min" in d:
                min_age = conf_get_duration(d["min"])
            if "max" in d:
                max_age = conf_get_duration(d["max"])
        has_age = min_age > 0 or max_age < MAX_AGE

        files = conf_get_paths(rm["files"]).search(childs=has_age)

        for file in files:
            if has_age:
                file_ts = int(os.path.getmtime(file) * 1000)
                current_ts = int(time.time() * 1000)
                if file_ts < current_ts - max_age or file_ts > current_ts - min_age: continue
            print("Delete '%s'" % file)
            try:
                send2trash(file.replace('/', '\\'))
            except FileNotFoundError as e:
                print(e)

    elif "run" in rule:
        run = rule["run"]
        founds = conf_get_paths(run["file"]).search(one=True)
        if len(founds) > 0: cmd = founds[0]
        else: cmd = run["file"]
        args = [cmd]
        if "args" in run:
            args.extend(run["args"])
        print("Run '%s'" % (' '.join(args)))
        try:
            subprocess.run(args, capture_output=True)
        except FileNotFoundError:
            print("Command '%s' not found" % args[0])



if __name__ == "__main__":
    rules = []

    with open("rules.json", "r", encoding="utf8") as file:
        rules = json.load(file)

    before = shutil.disk_usage("/").used
    for rule in rules:
        run_rule(rule)
    after = shutil.disk_usage("/").used

    print("\nProgram finished\nA total of %.3f MB has been freed" % ((before - after) / (1024*1024)))
