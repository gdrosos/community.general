#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (c) 2013, Jeroen Hoekx <jeroen.hoekx@dsquare.be>
# Copyright (c) 2016, Matt Robinson <git@nerdoftheherd.com>
# Copyright (c) 2017, Dag Wieers <dag@wieers.com>
# Copyright (c) 2017, Ansible Project
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
author:
  - Jeroen Hoekx (@jhoekx)
  - Matt Robinson (@ribbons)
  - Dag Wieers (@dagwieers)
module: iso_extract
short_description: Extract files from an ISO image
description:
  - This module has two possible ways of operation.
  - If 7zip is installed on the system, this module extracts files from an ISO into a temporary directory and copies files
    to a given destination, if needed.
  - If the user has mount-capabilities (CAP_SYS_ADMIN on Linux) this module mounts the ISO image to a temporary location,
    and copies files to a given destination, if needed.
requirements:
  - Either 7z (from C(7zip) or C(p7zip) package)
  - Or mount capabilities (root-access, or CAP_SYS_ADMIN capability on Linux)
extends_documentation_fragment:
  - community.general.attributes
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
options:
  image:
    description:
      - The ISO image to extract files from.
    type: path
    required: true
    aliases: [path, src]
  dest:
    description:
      - The destination directory to extract files to.
    type: path
    required: true
  files:
    description:
      - A list of files to extract from the image.
      - Extracting directories does not work.
    type: list
    elements: str
    required: true
  force:
    description:
      - If V(true), it replaces the remote file when contents are different than the source.
      - If V(false), the file is only extracted and copied if the destination does not already exist.
    type: bool
    default: true
  executable:
    description:
      - The path to the C(7z) executable to use for extracting files from the ISO.
      - If not provided, it assumes the value V(7z).
    type: path
  password:
    description:
      - Password used to decrypt files from the ISO.
      - It is only used if C(7z) is used.
      - The password is used as a command line argument to 7z. This is a B(potential security risk) that allows passwords
        to be revealed if someone else can list running processes on the same machine in the right moment.
    type: str
    version_added: 10.1.0
notes:
  - Only the file checksum (content) is taken into account when extracting files from the ISO image. If O(force=false), only
    checks the presence of the file.
"""

EXAMPLES = r"""
- name: Extract kernel and ramdisk from a LiveCD
  community.general.iso_extract:
    image: /tmp/rear-test.iso
    dest: /tmp/virt-rear/
    files:
      - isolinux/kernel
      - isolinux/initrd.cgz
"""

RETURN = r"""
#
"""

import os.path
import shutil
import tempfile

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec=dict(
            image=dict(type='path', required=True, aliases=['path', 'src']),
            dest=dict(type='path', required=True),
            files=dict(type='list', elements='str', required=True),
            force=dict(type='bool', default=True),
            password=dict(type='str', no_log=True),
            executable=dict(type='path'),  # No default on purpose
        ),
        supports_check_mode=True,
    )
    image = module.params['image']
    dest = module.params['dest']
    files = module.params['files']
    force = module.params['force']
    password = module.params['password']
    executable = module.params['executable']

    result = dict(
        changed=False,
        dest=dest,
        image=image,
    )

    # We want to know if the user provided it or not, so we set default here
    if executable is None:
        executable = '7z'

    binary = module.get_bin_path(executable, None)

    # When executable was provided and binary not found, warn user !
    if module.params['executable'] is not None and not binary:
        module.warn("Executable '%s' is not found on the system, trying to mount ISO instead." % executable)

    if not os.path.exists(dest):
        module.fail_json(msg="Directory '%s' does not exist" % dest)

    if not os.path.exists(os.path.dirname(image)):
        module.fail_json(msg="ISO image '%s' does not exist" % image)

    result['files'] = []
    extract_files = list(files)

    if not force:
        # Check if we have to process any files based on existence
        for f in files:
            dest_file = os.path.join(dest, os.path.basename(f))
            if os.path.exists(dest_file):
                result['files'].append(dict(
                    checksum=None,
                    dest=dest_file,
                    src=f,
                ))
                extract_files.remove(f)

    if not extract_files:
        module.exit_json(**result)

    tmp_dir = tempfile.mkdtemp()

    # Use 7zip when we have a binary, otherwise try to mount
    if binary:
        cmd = [binary, 'x', image, '-o%s' % tmp_dir]
        if password:
            cmd += ["-p%s" % password]
        cmd += extract_files
    else:
        cmd = [module.get_bin_path('mount'), '-o', 'loop,ro', image, tmp_dir]

    rc, out, err = module.run_command(cmd)
    if rc != 0:
        result.update(dict(
            cmd=cmd,
            rc=rc,
            stderr=err,
            stdout=out,
        ))
        shutil.rmtree(tmp_dir)

        if binary:
            module.fail_json(msg="Failed to extract from ISO image '%s' to '%s'" % (image, tmp_dir), **result)
        else:
            module.fail_json(msg="Failed to mount ISO image '%s' to '%s', and we could not find executable '%s'." % (image, tmp_dir, executable), **result)

    try:
        for f in extract_files:
            tmp_src = os.path.join(tmp_dir, f)
            if not os.path.exists(tmp_src):
                module.fail_json(msg="Failed to extract '%s' from ISO image" % f, **result)

            src_checksum = module.sha1(tmp_src)

            dest_file = os.path.join(dest, os.path.basename(f))

            if os.path.exists(dest_file):
                dest_checksum = module.sha1(dest_file)
            else:
                dest_checksum = None

            result['files'].append(dict(
                checksum=src_checksum,
                dest=dest_file,
                src=f,
            ))

            if src_checksum != dest_checksum:
                if not module.check_mode:
                    shutil.copy(tmp_src, dest_file)

                result['changed'] = True
    finally:
        if not binary:
            module.run_command([module.get_bin_path('umount'), tmp_dir])

        shutil.rmtree(tmp_dir)

    module.exit_json(**result)


if __name__ == '__main__':
    main()
