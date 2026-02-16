# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import paramiko
from daalu.bootstrap.ceph.models import CephHost
from daalu.utils.ssh_runner import SSHRunner
from daalu.bootstrap.node.models import Host


def open_ssh_1(host: Host) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=host.address,
        username=host.username,
        key_filename=host.pkey_path,
        timeout=30,
    )

    return client



def open_ssh(
    host: CephHost,
    *,
    connect_timeout: float = 20.0,
) -> SSHRunner:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pkey = None
    if host.pkey_path:
        for key_cls in (
            paramiko.RSAKey,
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
        ):
            try:
                pkey = key_cls.from_private_key_file(host.pkey_path)
                break
            except paramiko.SSHException:
                continue

    client.connect(
        hostname=host.address,
        port=host.port,
        username=host.username,
        password=host.password if not pkey else None,
        pkey=pkey,
        timeout=connect_timeout,
        allow_agent=True,
        look_for_keys=True,
    )

    return SSHRunner(client)
