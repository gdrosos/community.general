# -*- coding: utf-8 -*-
# Copyright (c) 2016 Dag Wieers <dag@wieers.com>
# Copyright (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r"""
name: filetree
author: Dag Wieers (@dagwieers) <dag@wieers.com>
short_description: Recursively match all files in a directory tree
description:
  - This lookup enables you to template a complete tree of files on a target system while retaining permissions and ownership.
  - Supports directories, files and symlinks, including SELinux and other file properties.
  - If you provide more than one path, it implements a first_found logic, and does not process entries it already processed
    in previous paths. This enables merging different trees in order of importance, or add role_vars to specific paths to
    influence different instances of the same role.
options:
  _terms:
    description: Path(s) of files to read.
    required: true
    type: list
    elements: string
"""

EXAMPLES = r"""
- name: Create directories
  ansible.builtin.file:
    path: /web/{{ item.path }}
    state: directory
    mode: '{{ item.mode }}'
  with_community.general.filetree: web/
  when: item.state == 'directory'

- name: Template files (explicitly skip directories in order to use the 'src' attribute)
  ansible.builtin.template:
    src: '{{ item.src }}'
    # Your template files should be stored with a .j2 file extension,
    # but should not be deployed with it. splitext|first removes it.
    dest: /web/{{ item.path | splitext | first }}
    mode: '{{ item.mode }}'
  with_community.general.filetree: web/
  when: item.state == 'file'

- name: Recreate symlinks
  ansible.builtin.file:
    src: '{{ item.src }}'
    dest: /web/{{ item.path }}
    state: link
    follow: false  # avoid corrupting target files if the link already exists
    force: true
    mode: '{{ item.mode }}'
  with_community.general.filetree: web/
  when: item.state == 'link'

- name: list all files under web/
  ansible.builtin.debug:
    msg: "{{ lookup('community.general.filetree', 'web/') }}"
"""

RETURN = r"""
_raw:
  description: List of dictionaries with file information.
  type: list
  elements: dict
  contains:
    src:
      description:
        - Full path to file.
        - Not returned when RV(_raw[].state) is set to V(directory).
      type: path
    root:
      description: Allows filtering by original location.
      type: path
    path:
      description: Contains the relative path to root.
      type: path
    mode:
      description: The permissions the resulting file or directory.
      type: str
    state:
      description: TODO.
      type: str
    owner:
      description: Name of the user that owns the file/directory.
      type: raw
    group:
      description: Name of the group that owns the file/directory.
      type: raw
    seuser:
      description: The user part of the SELinux file context.
      type: raw
    serole:
      description: The role part of the SELinux file context.
      type: raw
    setype:
      description: The type part of the SELinux file context.
      type: raw
    selevel:
      description: The level part of the SELinux file context.
      type: raw
    uid:
      description: Owner ID of the file/directory.
      type: int
    gid:
      description: Group ID of the file/directory.
      type: int
    size:
      description: Size of the target.
      type: int
    mtime:
      description: Time of last modification.
      type: float
    ctime:
      description: Time of last metadata update or creation (depends on OS).
      type: float
"""
import os
import pwd
import grp
import stat

HAVE_SELINUX = False
try:
    import selinux
    HAVE_SELINUX = True
except ImportError:
    pass

from ansible.plugins.lookup import LookupBase
from ansible.module_utils.common.text.converters import to_native, to_text
from ansible.utils.display import Display

display = Display()


# If selinux fails to find a default, return an array of None
def selinux_context(path):
    context = [None, None, None, None]
    if HAVE_SELINUX and selinux.is_selinux_enabled():
        try:
            # note: the selinux module uses byte strings on python2 and text
            # strings on python3
            ret = selinux.lgetfilecon_raw(to_native(path))
        except OSError:
            return context
        if ret[0] != -1:
            # Limit split to 4 because the selevel, the last in the list,
            # may contain ':' characters
            context = ret[1].split(':', 3)
    return context


def file_props(root, path):
    ''' Returns dictionary with file properties, or return None on failure '''
    abspath = os.path.join(root, path)

    try:
        st = os.lstat(abspath)
    except OSError as e:
        display.warning(f'filetree: Error using stat() on path {abspath} ({e})')
        return None

    ret = dict(root=root, path=path)

    if stat.S_ISLNK(st.st_mode):
        ret['state'] = 'link'
        ret['src'] = os.readlink(abspath)
    elif stat.S_ISDIR(st.st_mode):
        ret['state'] = 'directory'
    elif stat.S_ISREG(st.st_mode):
        ret['state'] = 'file'
        ret['src'] = abspath
    else:
        display.warning(f'filetree: Error file type of {abspath} is not supported')
        return None

    ret['uid'] = st.st_uid
    ret['gid'] = st.st_gid
    try:
        ret['owner'] = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        ret['owner'] = st.st_uid
    try:
        ret['group'] = to_text(grp.getgrgid(st.st_gid).gr_name)
    except KeyError:
        ret['group'] = st.st_gid
    ret['mode'] = f'0{stat.S_IMODE(st.st_mode):03o}'
    ret['size'] = st.st_size
    ret['mtime'] = st.st_mtime
    ret['ctime'] = st.st_ctime

    if HAVE_SELINUX and selinux.is_selinux_enabled() == 1:
        context = selinux_context(abspath)
        ret['seuser'] = context[0]
        ret['serole'] = context[1]
        ret['setype'] = context[2]
        ret['selevel'] = context[3]

    return ret


class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)

        basedir = self.get_basedir(variables)

        ret = []
        for term in terms:
            term_file = os.path.basename(term)
            dwimmed_path = self._loader.path_dwim_relative(basedir, 'files', os.path.dirname(term))
            path = os.path.join(dwimmed_path, term_file)
            display.debug(f"Walking '{path}'")
            for root, dirs, files in os.walk(path, topdown=True):
                for entry in dirs + files:
                    relpath = os.path.relpath(os.path.join(root, entry), path)

                    # Skip if relpath was already processed (from another root)
                    if relpath not in [entry['path'] for entry in ret]:
                        props = file_props(path, relpath)
                        if props is not None:
                            display.debug(f"  found '{os.path.join(path, relpath)}'")
                            ret.append(props)

        return ret
