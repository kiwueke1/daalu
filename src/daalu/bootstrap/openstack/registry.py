# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/registry.py

from pathlib import Path
from typing import List

from daalu.bootstrap.openstack.models import OpenStackSelection
from daalu.bootstrap.openstack.components.memcached import MemcachedComponent
from daalu.bootstrap.infrastructure.utils.assets import infra_asset_path
from daalu.bootstrap.openstack.components.keystone.keystone import KeystoneComponent
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakIAMConfig,
    KeycloakAdminAuth,
    KeycloakRealmSpec,
    KeycloakClientSpec,
    KeycloakDomainSpec,
)
from daalu.bootstrap.openstack.components.barbican.barbican import BarbicanComponent
from daalu.bootstrap.openstack.components.rook_ceph.rook_ceph import RookCephComponent
from daalu.bootstrap.openstack.components.rook_ceph.rook_ceph_cluster import RookCephClusterComponent
from daalu.bootstrap.openstack.components.rook_ceph.ceph_provisioners import  CephProvisionersComponent
from daalu.bootstrap.openstack.components.glance.glance import GlanceComponent
from daalu.bootstrap.openstack.components.staffeln.staffeln import StaffelnComponent
from daalu.bootstrap.openstack.components.cinder.cinder import CinderComponent
from daalu.bootstrap.openstack.components.placement.placement import PlacementComponent
from daalu.bootstrap.openstack.components.lpfc.lpfc import LpfcComponent
from daalu.bootstrap.openstack.components.multipathd.multipathd import MultipathdComponent
from daalu.bootstrap.openstack.components.iscsi.iscsi import IscsiComponent
from daalu.bootstrap.openstack.components.openvswitch.openvswitch import OpenvSwitchComponent
from daalu.bootstrap.openstack.components.frr_k8s.frr_k8s import FrrK8sComponent
from daalu.bootstrap.openstack.components.ovn.ovn import OvnComponent
from daalu.bootstrap.openstack.components.libvirt.libvirt import LibvirtComponent
from daalu.bootstrap.openstack.components.coredns.coredns import CoreDNSComponent
from daalu.bootstrap.openstack.components.heat.heat import HeatComponent
from daalu.bootstrap.openstack.components.ceilometer.ceilometer import CeilometerComponent
from daalu.bootstrap.openstack.components.neutron.neutron import NeutronComponent
from daalu.bootstrap.openstack.components.nova.nova import NovaComponent
from daalu.bootstrap.openstack.components.octavia.octavia import OctaviaComponent
from daalu.bootstrap.openstack.components.magnum.magnum import MagnumComponent
from daalu.bootstrap.openstack.components.manila.manila import ManilaComponent
from daalu.bootstrap.openstack.components.horizon.horizon import HorizonComponent
from daalu.bootstrap.openstack.components.openstack_exporter.openstack_exporter import OpenStackExporterComponent
from daalu.bootstrap.openstack.components.openstack_cli.openstack_cli import OpenStackCliComponent


def build_openstack_components(
    *,
    selection: OpenStackSelection,
    workspace_root: Path,
    kubeconfig_path: str,
    cfg,
    ssh=None,
    ceph_ssh=None,
):
    components: List = []
    secrets_path = workspace_root / "cloud-config" / "secrets.yaml"


    # ------------------------------------------------------------
    # Resolve Keycloak IAM config ONCE (shared by all components)
    # ------------------------------------------------------------
    keycloak_cfg = None
    openstack_cfg = getattr(cfg, "openstack", None)

    if getattr(cfg, "keycloak", None) and getattr(cfg.keycloak, "openstack", None):
        kc = cfg.keycloak.openstack

        keycloak_cfg = KeycloakIAMConfig(
            k8s_namespace="openstack",
            oauth2_proxy_ssl_insecure_skip_verify=kc.oauth2_proxy_ssl_insecure_skip_verify,
            oidc_issuer_url=kc.oidc_issuer_url,
            admin=KeycloakAdminAuth(
                base_url=kc.base_url,
                admin_realm=kc.admin_realm,
                admin_client_id=kc.admin_client_id,
                username=kc.username,
                password=kc.password,
                verify_tls=kc.verify_tls,
            ),
            realm=KeycloakRealmSpec(
                realm=kc.realm,
                display_name=kc.display_name,
                enabled=True,
            ),
            clients=[
                KeycloakClientSpec(
                    id="keystone",
                    redirect_uris=[
                        uri
                        for d in kc.domains
                        if d.client
                        for uri in d.client.redirect_uris
                    ] if kc.domains else [],
                ),
                KeycloakClientSpec(
                    id="grafana",
                    roles=["admin", "editor", "viewer"],
                    oauth2_proxy=True,
                    redirect_uris=kc.grafana_redirect_uris,
                    port=3000,
                ),
            ],
            domains=[
                KeycloakDomainSpec(
                    name=d.name,
                    label=d.label,
                    keycloak_realm=d.keycloak_realm,
                    totp_default_action=d.totp_default_action,
                    client=KeycloakClientSpec(**d.client.model_dump()),
                )
                for d in kc.domains
            ],
        )

    if selection.components is None or "memcached" in selection.components:
        components.append(
            MemcachedComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "memcached"),
                values_path=infra_asset_path(
                    workspace_root, "memcached", "values.yaml"
                ),
                enable_argocd=True,
            )
        )

    # ----------------------------
    # Keystone
    # ----------------------------
    if selection.components is None or "keystone" in selection.components:
        components.append(
            KeystoneComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "keystone"),
                values_path=infra_asset_path(
                    workspace_root, "keystone", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keycloak_config=keycloak_cfg,
                github_token=cfg.keycloak.openstack.github_token,
            )
        )

    # ----------------------------
    # Barbican
    # ----------------------------
    if selection.components is None or "barbican" in selection.components:
        components.append(
            BarbicanComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "barbican"),
                values_path=infra_asset_path(
                    workspace_root, "barbican", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Rook-Ceph
    # ----------------------------
    if selection.components is None or "rook-ceph" in selection.components:
        components.append(
            RookCephComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "rook-ceph"),
                values_path=infra_asset_path(workspace_root, "rook-ceph", "values.yaml"),
            )
        )

    if selection.components is None or "rook-ceph-cluster" in selection.components:
        components.append(
            RookCephClusterComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "rook-ceph-cluster"),
                values_path=infra_asset_path(
                    workspace_root, "rook-ceph-cluster", "values.yaml"
                ),
                secrets_path=secrets_path,
                ssh=ceph_ssh or ssh,
                rgw_public_host="object-store.daalu.io",
                ceph_image="quay.io/ceph/ceph:v18.2.0",
            )
        )


    if selection.components is None or "ceph-provisioners" in selection.components:
        components.append(
            CephProvisionersComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "ceph-provisioners"),
                values_path=infra_asset_path(
                    workspace_root, "ceph-provisioners", "values.yaml"
                ),
                ssh=ceph_ssh or ssh,
            )
        )

    # ----------------------------
    # Glance
    # ----------------------------
    if selection.components is None or "glance" in selection.components:
        components.append(
            GlanceComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "glance"),
                values_path=infra_asset_path(
                    workspace_root, "glance", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                glance_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Staffeln
    # ----------------------------
    if selection.components is None or "staffeln" in selection.components:
        components.append(
            StaffelnComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "staffeln"),
                values_path=infra_asset_path(
                    workspace_root, "staffeln", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Cinder
    # ----------------------------
    if selection.components is None or "cinder" in selection.components:
        components.append(
            CinderComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "cinder"),
                values_path=infra_asset_path(
                    workspace_root, "cinder", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Placement
    # ----------------------------
    if selection.components is None or "placement" in selection.components:
        components.append(
            PlacementComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "placement"),
                values_path=infra_asset_path(
                    workspace_root, "placement", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # lpfc (host kernel module tuning, SSH-based)
    # ----------------------------
    if ssh and (selection.components is None or "lpfc" in selection.components):
        components.append(
            LpfcComponent(
                kubeconfig=kubeconfig_path,
                ssh=ssh,
            )
        )

    # ----------------------------
    # multipathd (DM-Multipath config, SSH-based)
    # ----------------------------
    if ssh and (selection.components is None or "multipathd" in selection.components):
        components.append(
            MultipathdComponent(
                kubeconfig=kubeconfig_path,
                ssh=ssh,
            )
        )

    # ----------------------------
    # iscsi (iSCSI initiator daemon, SSH-based)
    # ----------------------------
    if ssh and (selection.components is None or "iscsi" in selection.components):
        components.append(
            IscsiComponent(
                kubeconfig=kubeconfig_path,
                ssh=ssh,
            )
        )

    # ----------------------------
    # SDN: Open vSwitch + OVN
    #
    # OVS is ALWAYS deployed (low-level virtual switch on every node).
    # OVN is the SDN control plane on top of OVS, deployed when
    # network_backend == "ovn" (the default).
    # FRR-K8s is optional BGP routing, only when ovn_bgp_agent is enabled.
    # ----------------------------
    network_backend = getattr(openstack_cfg, "network_backend", "ovn") if openstack_cfg else "ovn"
    ovn_bgp_agent_enabled = getattr(openstack_cfg, "ovn_bgp_agent_enabled", False) if openstack_cfg else False

    # OVS — always deployed
    if selection.components is None or "openvswitch" in selection.components:
        components.append(
            OpenvSwitchComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "openvswitch"),
                values_path=infra_asset_path(
                    workspace_root, "openvswitch", "values.yaml"
                ),
                ssh=ssh,
            )
        )

    # FRR-K8s — only when OVN BGP agent is enabled
    if ovn_bgp_agent_enabled and (selection.components is None or "frr-k8s" in selection.components):
        components.append(
            FrrK8sComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "frr-k8s"),
                values_path=infra_asset_path(
                    workspace_root, "frr-k8s", "values.yaml"
                ),
            )
        )

    # OVN — only when network_backend == "ovn"
    if network_backend == "ovn" and (selection.components is None or "ovn" in selection.components):
        components.append(
            OvnComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "ovn"),
                values_path=infra_asset_path(
                    workspace_root, "ovn", "values.yaml"
                ),
                ovn_bgp_agent_enabled=ovn_bgp_agent_enabled,
            )
        )

    # ----------------------------
    # Libvirt (hypervisor layer for Nova)
    # ----------------------------
    if selection.components is None or "libvirt" in selection.components:
        components.append(
            LibvirtComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "libvirt"),
                values_path=infra_asset_path(
                    workspace_root, "libvirt", "values.yaml"
                ),
                network_backend=network_backend,
                ssh=ssh,
            )
        )

    # ----------------------------
    # CoreDNS (Neutron DNS resolver)
    # ----------------------------
    if selection.components is None or "coredns" in selection.components:
        components.append(
            CoreDNSComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "coredns"),
                values_path=infra_asset_path(
                    workspace_root, "coredns", "values.yaml"
                ),
            )
        )

    # ----------------------------
    # Neutron (OpenStack Networking)
    # ----------------------------
    if selection.components is None or "neutron" in selection.components:
        components.append(
            NeutronComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "neutron"),
                values_path=infra_asset_path(
                    workspace_root, "neutron", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
                network_backend=network_backend,
            )
        )

    # ----------------------------
    # Nova (OpenStack Compute)
    # ----------------------------
    if selection.components is None or "nova" in selection.components:
        components.append(
            NovaComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "nova"),
                values_path=infra_asset_path(
                    workspace_root, "nova", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
                network_backend=network_backend,
                nova_flavors=[
                    {"name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1},
                    {"name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20},
                    {"name": "m1.medium", "vcpus": 2, "ram": 4096, "disk": 40},
                    {"name": "m1.large", "vcpus": 4, "ram": 8192, "disk": 80},
                    {"name": "m1.xlarge", "vcpus": 8, "ram": 16384, "disk": 160},
                ],
            )
        )

    # ----------------------------
    # Heat (OpenStack Orchestration)
    # ----------------------------
    if selection.components is None or "heat" in selection.components:
        components.append(
            HeatComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "heat"),
                values_path=infra_asset_path(
                    workspace_root, "heat", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Ceilometer (OpenStack Telemetry)
    # ----------------------------
    if selection.components is None or "ceilometer" in selection.components:
        components.append(
            CeilometerComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "ceilometer"),
                values_path=infra_asset_path(
                    workspace_root, "ceilometer", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Octavia (OpenStack Load Balancer)
    # ----------------------------
    if selection.components is None or "octavia" in selection.components:
        components.append(
            OctaviaComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "octavia"),
                values_path=infra_asset_path(
                    workspace_root, "octavia", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Magnum (OpenStack Container Infrastructure)
    # ----------------------------
    if selection.components is None or "magnum" in selection.components:
        components.append(
            MagnumComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "magnum"),
                values_path=infra_asset_path(
                    workspace_root, "magnum", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Manila (OpenStack Shared File System)
    # ----------------------------
    if selection.components is None or "manila" in selection.components:
        components.append(
            ManilaComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "manila"),
                values_path=infra_asset_path(
                    workspace_root, "manila", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # Horizon (OpenStack Dashboard)
    # ----------------------------
    if selection.components is None or "horizon" in selection.components:
        components.append(
            HorizonComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "horizon"),
                values_path=infra_asset_path(
                    workspace_root, "horizon", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    # ----------------------------
    # OpenStack Exporter (Prometheus metrics)
    # ----------------------------
    if selection.components is None or "openstack-exporter" in selection.components:
        components.append(
            OpenStackExporterComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
            )
        )

    # ----------------------------
    # OpenStack CLI (host-level CLI config, SSH-based)
    # ----------------------------
    if ssh and (selection.components is None or "openstack-cli" in selection.components):
        components.append(
            OpenStackCliComponent(
                kubeconfig=kubeconfig_path,
                ssh=ssh,
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keystone_public_host=str(cfg.keycloak.openstack.base_url)
                    .replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/"),
            )
        )

    return components
