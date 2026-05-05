# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/keystone/keystone.py

from pathlib import Path
import time
import json
import requests
from typing import Any
import shlex
import base64

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.iam.keycloak import KeycloakIAMManager
from daalu.bootstrap.infrastructure.components.kube_coredns import KubeCoreDNSPatchComponent
from daalu.utils.serialize import to_jsonable
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakRealmSpec,
    KeycloakDomainSpec,
    KeycloakClientSpec,
    KeycloakAdminAuth,
)
from daalu.bootstrap.shared.keycloak.iam import (
    KeycloakIAMManager as SharedKeycloakIAMManager,
    KeycloakIAMConfig,
)
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.bootstrap.openstack.endpoints import (
    OpenStackHelmEndpoints,
)
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")



class KeystoneComponent(InfraComponent):
    """
    Daalu Keystone component

    Mirrors exactly:
      roles/keystone/tasks/main.yml
      roles/keystone/tasks/argocd_onboard.yml
    """

    # -------------------------------------------------
    # Constructor
    # -------------------------------------------------
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        keycloak_config: KeycloakIAMConfig,
        github_token: str,
        secrets_path: Path,
        cluster_name: str = "default", 
    ):
        super().__init__(
            name="keystone",
            repo_name="local",
            repo_url="",
            chart="keystone",
            version=None,
            namespace=namespace,
            release_name="keystone",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/keystone"),
            kubeconfig=kubeconfig,
            istio_enabled=True,
            istio_host="identity.daalu.io",
            istio_service="keystone-api",
            istio_service_namespace="openstack",
            istio_service_port=5000,
            istio_expected_status=300,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.keycloak_config = keycloak_config
        self.github_token = github_token

        self._iam: KeycloakIAMManager | None = None

        self.wait_for_pods = True
        self.min_running_pods = 1

        self.secrets_path = secrets_path
        self.cluster_name = cluster_name


        self.keycloak_cfg = keycloak_config
        self._iam = None
        self.requires_public_ingress = True

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir


    def values(self) -> dict:
        base = self.load_values_file(self.values_path)

        if not hasattr(self, "_computed_endpoints"):
            raise RuntimeError("OpenStack endpoints not computed yet")

        base["endpoints"] = self._computed_endpoints

        # Derive horizon FQDN from keystone istio_host (e.g. identity.daalu.io → dashboard.daalu.io)
        keystone_fqdn = self.istio_host
        base_domain = ".".join(keystone_fqdn.split(".")[1:])
        horizon_fqdn = f"dashboard.{base_domain}"

        # Inject OIDC secrets and hostnames into the Apache wsgi_keystone config
        if hasattr(self, "_oidc_crypto_passphrase") and hasattr(self, "_oidc_client_secret"):
            wsgi = base.get("conf", {}).get("wsgi_keystone", "")
            if wsgi:
                wsgi = wsgi.replace("CHANGE_ME_OIDC_CRYPTO_PASSPHRASE", self._oidc_crypto_passphrase)
                wsgi = wsgi.replace("CHANGE_ME_OIDC_CLIENT_SECRET", self._oidc_client_secret)
                wsgi = wsgi.replace("CHANGE_ME_KEYSTONE_FQDN", keystone_fqdn)
                wsgi = wsgi.replace("CHANGE_ME_HORIZON_FQDN", horizon_fqdn)
                base["conf"]["wsgi_keystone"] = wsgi

        # Inject trusted_dashboard with the correct Horizon public FQDN
        try:
            td = base["conf"]["keystone"]["federation"]["trusted_dashboard"]
            td["values"] = [
                v.replace("CHANGE_ME_HORIZON_FQDN", horizon_fqdn)
                for v in td.get("values", [])
            ]
        except (KeyError, TypeError):
            pass

        return base

    # -------------------------------------------------
    # 1) Create Keycloak realms
    # -------------------------------------------------
    def _ensure_keycloak_realm(self):
        log.debug("[keystone] Ensuring Keycloak realm...")
        self._iam.ensure_realm()
        log.debug("[keystone] Keycloak realm ensured ✓")

    def _create_keycloak_realms(self):
        log.debug("[keystone] Creating Keycloak realms...")
        for domain in self._iter_domains():
            self._iam.ensure_realm(
                realm=domain.keycloak_realm,
                display_name=domain.label,
            )
        log.debug("[keystone] Keycloak realms created ✓")

    def _ensure_iam(self):
        if self._iam is not None:
            return

        if not self.keycloak_cfg:
            raise RuntimeError(
                "Keycloak IAM requested but keycloak config is missing"
            )

        # At this point keycloak_cfg is already a KeycloakIAMConfig
        if not isinstance(self.keycloak_cfg, KeycloakIAMConfig):
            raise TypeError(
                f"Expected KeycloakIAMConfig, got {type(self.keycloak_cfg)}"
            )

        self._iam = KeycloakIAMManager(config=self.keycloak_cfg)
        self._iam.login()


    # -------------------------------------------------
    # 2) Setup MFA required actions
    # -------------------------------------------------
    def _setup_keycloak_required_actions(self):
        log.debug("[keystone] Configuring Keycloak required actions (MFA)...")
        for domain in self._iter_domains():
            self._iam.ensure_required_action(
                realm=domain.keycloak_realm,
                alias="CONFIGURE_TOTP",
                name="Configure OTP",
                enabled=True,
                default_action=domain.totp_default_action,
            )
        log.debug("[keystone] Required actions configured ✓")




    # -------------------------------------------------
    # 3) Create OpenID metadata ConfigMap
    # -------------------------------------------------
    def _create_openid_metadata_configmap(self, kubectl):
        log.debug("[keystone] Creating OpenID metadata ConfigMap...")
        template = self.assets_dir() / "templates/configmap-openid-metadata.yml.j2"
        kubectl.apply_template(
            template=template,
            namespace=self.namespace,
        )
        log.debug("[keystone] OpenID metadata ConfigMap created ✓")

    # -------------------------------------------------
    # 4) Create Keycloak clients
    # -------------------------------------------------
    def _create_keycloak_client(self):
        log.debug("[keystone] Ensuring Keystone Keycloak client...")
        client = next(
            c for c in self.keycloak_config.clients if c.id == "keystone"
        )

        client_uuid = self._iam.ensure_client(client)
        log.debug("[keystone] Keystone client ensured ✓")

        return client_uuid


    # -------------------------------------------------
    # 5) Deploy Keystone Helm chart
    # -------------------------------------------------
    def _deploy_keystone_helm(self):
        log.debug("[keystone] Deploying Keystone Helm chart...")
        self.helm_install(values=self.values())
        log.debug("[keystone] Keystone Helm deployed ✓")


    # -------------------------------------------------
    # 8) Wait for Keystone API ready
    # -------------------------------------------------
    def _wait_for_keystone_ready(self, kubectl):
        log.debug("[keystone] Waiting for keystone-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="keystone-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[keystone] Keystone API ready ✓")

    # -------------------------------------------------
    # 9) Create Keystone domains
    # -------------------------------------------------
    def _create_keystone_domains(self):
        log.debug("[keystone] Creating Keystone domains...")
        for domain in self._iter_domains():
            log.debug(f"domain is {domain}")
            self._ensure_domain_in_keystone(domain)
        log.debug("[keystone] Keystone domains created")


    def _ensure_single_keystone_domain(self, domain: KeycloakDomainSpec):
        """
        A Keystone domain is ensured by:
        - ensuring Keycloak realm exists (already handled by IAM in pre_install)
        - ensuring Keystone domain exists (created via Helm ks_domains)
        - wiring identity provider later (post_install steps)
        """

        # Verify Keystone domain was created by Helm
        self._ensure_domain_in_keystone(domain)


    def _ensure_domain_in_keystone(self, domain: KeycloakDomainSpec):
        """
        Verify that the Keystone domain exists.
        Domains are created declaratively via Helm (ks_domains).

        The keystone-api container does NOT have OS_* env vars
        (only bootstrap/domain-manage Jobs do), so we must inject
        credentials from the computed endpoints when exec-ing into it.
        """

        pod = self._get_keystone_api_pod()
        openrc = self._build_openrc_env()

        # Build an 'env K=V ...' prefix so the openstack CLI
        # picks up auth inside the container.
        env_parts = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_parts} "
            f"openstack domain show {domain.name} -f json"
        )

        rc, out, err = self.kubectl._run(cmd)

        if rc != 0:
            raise RuntimeError(
                f"Keystone domain '{domain.name}' not found. "
                f"Expected it to be created via Helm ks_domains.\n"
                f"{err or out}"
            )

        try:
            data = json.loads(out)
            log.debug(f"[keystone] Domain verified: {data.get('name')}")
        except Exception:
            log.debug(f"[keystone] Domain '{domain.name}' verified")

    def _build_openrc_env(self) -> dict[str, str]:
        """
        Build OS_* env vars from the already-computed endpoints.
        These mirror what helm-toolkit injects into bootstrap Jobs
        via the keystone-keystone-admin Secret.
        """
        admin = self._computed_endpoints["identity"]["auth"]["admin"]
        host = self._computed_endpoints["identity"]["hosts"]["default"]
        port = self._computed_endpoints["identity"]["port"]["api"]["default"]

        return {
            "OS_IDENTITY_API_VERSION": "3",
            "OS_AUTH_URL": f"http://{host}:{port}/v3",
            "OS_REGION_NAME": admin["region_name"],
            "OS_INTERFACE": "internal",
            "OS_PROJECT_DOMAIN_NAME": admin["project_domain_name"],
            "OS_PROJECT_NAME": admin["project_name"],
            "OS_USER_DOMAIN_NAME": admin["user_domain_name"],
            "OS_USERNAME": admin["username"],
            "OS_PASSWORD": admin["password"],
            "OS_DEFAULT_DOMAIN": admin.get("default_domain_id", "default"),
        }

    def _exec_keystone(self, cmd: list[str]):
        """
        Execute a command inside the keystone-api pod.
        """
        pod = self._get_keystone_api_pod()

        return self.kubectl.exec(
            pod=pod,
            namespace=self.namespace,
            command=cmd,
            container="keystone-api",
            check=True,
        )

    def _get_keystone_api_pod(self) -> str:
        pods = self.kubectl.get_pods(self.namespace)

        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            if (
                labels.get("application") == "keystone"
                and labels.get("component") == "api"
            ):
                return pod["metadata"]["name"]

        raise RuntimeError("No keystone-api pod found")


    # -------------------------------------------------
    # 10) Create identity providers
    # -------------------------------------------------
    def _create_identity_providers(self):
        log.debug("[keystone] Creating identity providers...")
        openrc = self._build_openrc_env()
        pod = self._get_keystone_api_pod()

        for domain in self._iter_domains():
            idp_name = domain.name
            # Strip any trailing slash from base_url before building the remote_id
            # so we never produce a double-slash like "auth.daalu.io//realms/daalu".
            # Keycloak's JWT "iss" claim is always single-slash; a mismatch causes
            # Keystone to reject federation with "Error in handling response type".
            base = str(self.keycloak_config.admin.base_url).rstrip("/")
            remote_id = f"{base}/realms/{domain.keycloak_realm}"

            env_prefix = " ".join(
                f"{k}={shlex.quote(v)}" for k, v in openrc.items()
            )
            check_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} "
                f"openstack identity provider show {idp_name} -f json"
            )
            rc, out, err = self.kubectl._run(check_cmd)

            if rc == 0:
                # IDP exists — always sync the remote_id so it stays correct
                # even if it was created with a wrong value (e.g. double slash).
                log.debug(
                    f"[keystone] IDP '{idp_name}' already exists — "
                    f"syncing remote_id to '{remote_id}'"
                )
                update_cmd = (
                    f"exec {pod} -n {self.namespace} -c keystone-api -- "
                    f"env {env_prefix} "
                    f"openstack identity provider set {idp_name} "
                    f"--remote-id {shlex.quote(remote_id)}"
                )
                self.kubectl._run(update_cmd)
                continue

            create_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} "
                f"openstack identity provider create {idp_name} "
                f"--remote-id {shlex.quote(remote_id)} "
                f"--domain {shlex.quote(domain.name)}"
            )
            rc, out, err = self.kubectl._run(create_cmd)

            if rc != 0:
                raise RuntimeError(
                    f"Failed to create identity provider '{idp_name}': {err or out}"
                )
            log.debug(f"[keystone] IDP '{idp_name}' created")

        log.debug("[keystone] Identity providers created")


    # -------------------------------------------------
    # 11) Create federated group and project
    # -------------------------------------------------
    def _create_federated_group_and_project(self):
        """
        Create the Keystone group and project that federated (SSO) users land in.

        Keystone federation mappings assign users to *groups*, not directly to
        roles.  A group must already exist with a role assignment on a project
        before any federated user can receive an OpenStack token scoped to that
        project.  Without this, Horizon can redirect back from Keycloak but
        Keystone returns "Error in handling response type" or the user has no
        accessible projects.

        For each configured Keycloak domain we create:
          • group  ``federated-users``  in the Keystone domain  (e.g. daalu)
          • project ``daalu-default``   in the Keystone domain
          • ``member`` role assignment: federated-users → daalu-default
        """
        log.debug("[keystone] Creating federated group/project resources...")
        openrc = self._build_openrc_env()
        pod = self._get_keystone_api_pod()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        def _exec(subcmd: str) -> tuple[int, str, str]:
            return self.kubectl._run(
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} {subcmd}"
            )

        for domain in self._iter_domains():
            domain_name = domain.name
            group_name = "federated-users"
            project_name = f"{domain_name}-default"

            # --- group ---
            rc, out, _ = _exec(
                f"openstack group show {group_name} --domain {domain_name} -f json"
            )
            if rc != 0:
                rc, out, err = _exec(
                    f"openstack group create {group_name} --domain {domain_name} -f json"
                )
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to create group '{group_name}' in domain "
                        f"'{domain_name}': {err or out}"
                    )
                log.debug("[keystone] Group '%s' created in domain '%s'", group_name, domain_name)
            else:
                log.debug("[keystone] Group '%s' already exists", group_name)

            # --- project ---
            rc, out, _ = _exec(
                f"openstack project show {project_name} --domain {domain_name} -f json"
            )
            if rc != 0:
                rc, out, err = _exec(
                    f"openstack project create {project_name} --domain {domain_name} -f json"
                )
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to create project '{project_name}' in domain "
                        f"'{domain_name}': {err or out}"
                    )
                log.debug("[keystone] Project '%s' created in domain '%s'", project_name, domain_name)
            else:
                log.debug("[keystone] Project '%s' already exists", project_name)

            # --- role assignment (idempotent — openstack role add is a no-op if already assigned) ---
            _exec(
                f"openstack role add member "
                f"--group {shlex.quote(group_name)} "
                f"--group-domain {shlex.quote(domain_name)} "
                f"--project {shlex.quote(project_name)} "
                f"--project-domain {shlex.quote(domain_name)}"
            )
            log.debug(
                "[keystone] 'member' role assigned to group '%s' on project '%s'",
                group_name,
                project_name,
            )

        log.debug("[keystone] Federated group/project resources ready")

    # -------------------------------------------------
    # 11b) Sync existing shadow users into the group
    # -------------------------------------------------
    def _sync_shadow_users_to_group(self):
        """
        Add every existing shadow user in the federated domain to the
        ``federated-users`` Keystone group persistently (i.e. in the DB, not
        just inside a token).

        WHY THIS IS NEEDED
        ------------------
        The federation mapping places users into ``federated-users`` only for
        the duration of the token.  Keystone's application-credential API
        validates roles by querying the database for role assignments, so
        token-only group membership is invisible to it.  Without a persistent
        DB group membership, federated users see:

            Invalid application credential: Could not find role assignment
            with role: …, user or group: …, project …

        This method is idempotent — adding a user who is already in the group
        is a no-op (openstack CLI exits 0 silently).

        WHEN TO CALL
        ------------
        • Called automatically at the end of ``post_install`` to handle any
          users who logged in during or before the bootstrap run.
        • Re-run manually via the CLI after new users log in for the first
          time (Keycloak SSO creates the shadow user on first login).
        """
        log.info("[keystone] Syncing shadow users into federated-users group...")
        openrc = self._build_openrc_env()
        pod = self._get_keystone_api_pod()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        def _exec(subcmd: str) -> tuple[int, str, str]:
            return self.kubectl._run(
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} {subcmd}"
            )

        for domain in self._iter_domains():
            domain_name = domain.name
            group_name = "federated-users"

            # List all users in the federated domain
            rc, out, err = _exec(
                f"openstack user list --domain {shlex.quote(domain_name)} -f json"
            )
            if rc != 0:
                log.warning(
                    "[keystone] Could not list users in domain '%s': %s",
                    domain_name, err or out,
                )
                continue

            try:
                users = json.loads(out)
            except Exception:
                log.warning("[keystone] Failed to parse user list JSON")
                continue

            for user in users:
                username = user.get("Name") or user.get("name", "")
                if not username:
                    continue

                # openstack group add user is idempotent — no-op if already a member
                rc, _, err = _exec(
                    f"openstack group add user {shlex.quote(group_name)} "
                    f"{shlex.quote(username)} "
                    f"--group-domain {shlex.quote(domain_name)} "
                    f"--user-domain {shlex.quote(domain_name)}"
                )
                if rc == 0:
                    log.debug(
                        "[keystone] User '%s' added to group '%s' in domain '%s'",
                        username, group_name, domain_name,
                    )
                else:
                    log.warning(
                        "[keystone] Could not add user '%s' to group '%s': %s",
                        username, group_name, err,
                    )

        log.info("[keystone] Shadow user sync complete")

    # -------------------------------------------------
    # 11c) Deploy CronJob that keeps shadow users in sync automatically
    # -------------------------------------------------
    def _deploy_shadow_user_sync_cronjob(self, kubectl) -> None:
        """
        Deploy (or update) a Kubernetes CronJob that runs every 5 minutes and
        adds every shadow user in each federated Keystone domain to the
        ``federated-users`` group persistently.

        WHY A CRONJOB IS NEEDED
        -----------------------
        Keystone creates a "shadow user" DB record the first time a federated
        (SSO) user logs in through Keycloak.  The federation mapping places the
        user into ``federated-users`` only for the lifetime of the token — the
        group membership is NOT written to the DB.

        Keystone's application-credential API validates roles by querying the DB
        for group membership, so token-only membership is invisible to it.
        Without persistent DB membership, a freshly-logged-in SSO user sees:

            Invalid application credential: Could not find role assignment ...

        This CronJob closes the gap: within ~5 minutes of a user's first login
        their shadow user is persistently added to ``federated-users`` and they
        can create Application Credentials without any admin intervention.

        The sync is fully idempotent — ``openstack group add user`` is a no-op
        when the user is already a group member.
        """
        log.info("[keystone] Deploying shadow-user sync CronJob...")

        # Prefer the same image as the live keystone-api pods for CLI
        # version consistency.  Fall back to the image from values.yaml.
        rc, out, _ = kubectl._run(
            "get deployment keystone-api -n openstack"
            " -o jsonpath='{.spec.template.spec.containers[0].image}'"
        )
        keystone_image = (
            out.strip().strip("'\"")
            if rc == 0 and out.strip()
            else "10.10.0.9:30003/openstack/keystone:2024.2-oidc"
        )

        openrc = self._build_openrc_env()
        domain_names = [d.name for d in self._iter_domains()]

        # Build the bash array literal, e.g.: daalu 'other-domain'
        domains_bash = " ".join(shlex.quote(d) for d in domain_names)

        # Pure bash script — avoids quoting edge-cases from inline Python.
        # openstack group add user exits 0 whether or not the user is already
        # a member, so the loop is fully idempotent.
        sync_script = (
            "#!/bin/bash\n"
            "set -euo pipefail\n\n"
            "DOMAINS=(" + domains_bash + ")\n\n"
            'for DOMAIN in "${DOMAINS[@]}"; do\n'
            '    echo "=== Syncing domain: $DOMAIN ==="\n'
            '    openstack user list --domain "$DOMAIN" -f value -c Name'
            " 2>/dev/null | while IFS= read -r USERNAME; do\n"
            '        [[ -z "$USERNAME" ]] && continue\n'
            '        openstack group add user federated-users "$USERNAME"'
            ' --group-domain "$DOMAIN" --user-domain "$DOMAIN"'
            ' && echo "  [ok] $USERNAME"'
            ' || echo "  [skip] $USERNAME"\n'
            "    done\n"
            "done\n"
            'echo "Sync complete."\n'
        )

        env_block = [{"name": k, "value": v} for k, v in openrc.items()]

        cronjob = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": "keystone-federated-user-sync",
                "namespace": self.namespace,
                "labels": {
                    "app": "keystone-federated-user-sync",
                    "component": "federation",
                },
            },
            "spec": {
                "schedule": "*/5 * * * *",
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 3,
                "failedJobsHistoryLimit": 3,
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "restartPolicy": "OnFailure",
                                "containers": [
                                    {
                                        "name": "sync",
                                        "image": keystone_image,
                                        "command": ["/bin/bash", "-c", sync_script],
                                        "env": env_block,
                                    }
                                ],
                            }
                        }
                    }
                },
            },
        }

        kubectl.apply_objects(
            [cronjob],
            remote_path="/tmp/daalu-keystone-federated-sync-cronjob.yaml",
        )
        log.info("[keystone] CronJob 'keystone-federated-user-sync' deployed ✓")

    # -------------------------------------------------
    # 12) Create federation mappings
    # -------------------------------------------------
    def _create_federation_mappings(self):
        """
        Create (or update) the Keystone federation mapping for each domain.

        The mapping translates OIDC claims from Keycloak into a Keystone user
        and group identity:
          - ``OIDC-preferred_username``  →  ephemeral user name  (daalu domain)
          - automatically adds the user to the ``federated-users`` group so they
            inherit the ``member`` role on the ``daalu-default`` project.

        The mapping is always synced (using ``openstack mapping set``) so that
        adding a new attribute mapping on a re-run is picked up without having
        to manually delete the old mapping first.
        """
        log.debug("[keystone] Syncing federation mappings...")
        openrc = self._build_openrc_env()
        pod = self._get_keystone_api_pod()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )
        for domain in self._iter_domains():
            mapping_name = f"{domain.name}-mapping"

            rules = json.dumps([
                {
                    "local": [
                        # Ephemeral user in the domain — name taken from the
                        # Keycloak "preferred_username" OIDC claim.
                        {
                            "user": {
                                "name": "{0}",
                                "domain": {"name": domain.name},
                            },
                        },
                        # Assign the user to the federated-users group so they
                        # inherit project roles (created in _create_federated_group_and_project).
                        {
                            "group": {
                                "name": "federated-users",
                                "domain": {"name": domain.name},
                            },
                        },
                    ],
                    "remote": [
                        {"type": "OIDC-preferred_username"},
                    ],
                }
            ])

            # Check if mapping already exists to choose create vs set.
            check_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} "
                f"openstack mapping show {mapping_name} -f json"
            )
            rc, _, _ = self.kubectl._run(check_cmd)
            action = "set" if rc == 0 else "create"

            rules_b64 = base64.b64encode(rules.encode()).decode()
            apply_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f'bash -c "echo -n {rules_b64} | base64 -d > /tmp/mapping-rules.json '
                f"&& env {env_prefix} "
                f"openstack mapping {action} {mapping_name} "
                f'--rules /tmp/mapping-rules.json"'
            )
            rc, out, err = self.kubectl._run(apply_cmd)
            if rc != 0:
                raise RuntimeError(
                    f"Failed to {action} mapping '{mapping_name}': {err or out}"
                )
            log.debug(f"[keystone] Mapping '{mapping_name}' {action}d")
        log.debug("[keystone] Federation mappings synced")

    # -------------------------------------------------
    # 12) Create federation protocols
    # -------------------------------------------------
    def _create_federation_protocols(self):
        log.debug("[keystone] Creating federation protocols...")
        openrc = self._build_openrc_env()
        pod = self._get_keystone_api_pod()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        for domain in self._iter_domains():
            idp_name = domain.name
            mapping_name = f"{domain.name}-mapping"

            # Check if protocol already exists
            check_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} "
                f"openstack federation protocol show openid "
                f"--identity-provider {idp_name} -f json"
            )
            rc, out, err = self.kubectl._run(check_cmd)

            if rc == 0:
                log.debug(f"[keystone] Protocol 'openid' for IDP '{idp_name}' already exists")
                continue

            create_cmd = (
                f"exec {pod} -n {self.namespace} -c keystone-api -- "
                f"env {env_prefix} "
                f"openstack federation protocol create openid "
                f"--identity-provider {idp_name} "
                f"--mapping {mapping_name}"
            )
            rc, out, err = self.kubectl._run(create_cmd)

            if rc != 0:
                raise RuntimeError(
                    f"Failed to create federation protocol for '{idp_name}': {err or out}"
                )
            log.debug(f"[keystone] Protocol 'openid' for IDP '{idp_name}' created")

        log.debug("[keystone] Federation protocols created")


    # -------------------------------------------------
    # 13) Argo CD onboarding
    # -------------------------------------------------
    def _onboard_argocd(self, kubectl):
        log.debug("[keystone] Checking Argo CD onboarding...")
        apps = kubectl.get_argocd_apps()

        if "keystone" in [a.lower() for a in apps]:
            log.debug("[keystone] Already onboarded to Argo CD ✓")
            return

        log.debug("[keystone] Onboarding Keystone to Argo CD...")
        url = (
            "https://api.github.com/repos/kiwueke1/"
            "argocd-infrastructure-app/contents/apps/openstack/keystone/keystone.yaml"
        )

        r = requests.get(
            url,
            headers={
                "Accept": "application/vnd.github.v3.raw",
                "Authorization": f"token {self.github_token}",
            },
            timeout=10,
        )
        r.raise_for_status()

        kubectl.apply_yaml(
            content=r.text,
            kubeconfig="/etc/kubernetes/admin.conf",
        )
        log.debug("[keystone] Keystone onboarded to Argo CD ✓")

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------


    def _cleanup_stale_jobs(self, kubectl):
        """Delete stale Helm hook jobs so they can be re-created on next install."""
        stale_jobs = [
            "keystone-db-init",
            "keystone-db-sync",
            "keystone-rabbit-init",
            "keystone-fernet-setup",
            "keystone-credential-setup",
            "keystone-bootstrap",
            "keystone-domain-manage",
        ]
        for job_name in stale_jobs:
            rc, _, _ = kubectl.run(
                ["get", "job", job_name, "-n", self.namespace, "-o", "name"],
            )
            if rc == 0:
                log.debug("[keystone] Deleting stale job: %s", job_name)
                kubectl.run(
                    ["delete", "job", job_name, "-n", self.namespace, "--ignore-not-found=true"]
                )

    def pre_install(self, kubectl):
        log.debug("[keystone] Starting pre-install...")

        # -------------------------------------------------
        # 0) Re-apply CoreDNS split-horizon patch (idempotent)
        # -------------------------------------------------
        # The keystone-api pod's Apache mod_auth_openidc module makes outbound
        # HTTPS calls to https://auth.daalu.io/realms/daalu/.well-known/openid-configuration.
        # Without this patch, pods resolve auth.daalu.io to the pfSense WAN IP (hairpin NAT),
        # get its self-signed cert, and return HTTP 500 ("Internal Server Error") on the
        # WebSSO login page.  The CoreDNS patch is also applied at the end of infrastructure
        # bootstrap, but it must be re-applied here because the ConfigMap can be reset by
        # Kubernetes upgrades or CoreDNS updates, causing the same failure to recur.
        log.info("[keystone] Re-applying CoreDNS split-horizon patch...")
        try:
            KubeCoreDNSPatchComponent(kubeconfig=self.kubeconfig).post_install(kubectl)
        except Exception as e:
            log.warning("[keystone] CoreDNS patch failed (will continue): %s", e)

        # -------------------------------------------------
        # 0b) Clean up stale Helm hook jobs from previous runs
        # -------------------------------------------------
        self._cleanup_stale_jobs(kubectl)

        # -------------------------------------------------
        # 1) Generate OpenStack Helm endpoints (DB, Rabbit, Cache)
        # -------------------------------------------------
        log.debug("[keystone] Building OpenStack Helm endpoints...")

        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )

        rmq.ensure_cluster("keystone")

        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.istio_host,
            service="keystone",
        )

        log.debug("[keystone] OpenStack endpoints ready ✓")

        # -------------------------------------------------
        # 2) Read OIDC secrets for Apache mod_auth_openidc
        # -------------------------------------------------
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._oidc_crypto_passphrase = secrets.require("keystone_oidc_crypto_passphrase")
        self._oidc_client_secret = secrets.require("keystone_keycloak_client_secret")
        log.debug("[keystone] OIDC secrets loaded ✓")

        # -------------------------------------------------
        # DEBUG 1: Dump computed OpenStack endpoints
        # -------------------------------------------------
        log.debug("[keystone][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(
            json.dumps(
                to_jsonable(self._computed_endpoints),
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

        # -------------------------------------------------
        # DEBUG 2: Dump FINAL Helm values (values.yaml + endpoints)
        # -------------------------------------------------
        values = self.values()

        # If your Helm engine merges endpoints later, expose them explicitly
        values_with_endpoints = dict(values)
        values_with_endpoints.setdefault("endpoints", {})
        values_with_endpoints["endpoints"].update(self._computed_endpoints)

        log.debug("[keystone][DEBUG] FINAL Keystone Helm values (pre-install):")
        log.debug(
            json.dumps(
                values_with_endpoints,
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

        # -------------------------------------------------
        # DEBUG 3: Focused OpenRC / auth values (Helm Toolkit failure zone)
        # -------------------------------------------------
        log.debug("[keystone][DEBUG] Keystone OpenRC-related values:")
        log.debug(
            json.dumps(
                {
                    "endpoints.identity": (
                        values_with_endpoints
                        .get("endpoints", {})
                        .get("identity")
                    ),
                    "conf.keystone.auth": (
                        values_with_endpoints
                        .get("conf", {})
                        .get("keystone", {})
                        .get("auth")
                    ),
                },
                indent=2,
                default=str,
            )
        )


        # -------------------------------------------------
        # 4) Keycloak IAM bootstrap (PRE-HELM)
        # -------------------------------------------------
        for iam in self._iter_iam_managers():
            iam.run(kubectl)

        # -------------------------------------------------
        # 5) Override _oidc_client_secret with actual Keycloak secret
        # -------------------------------------------------
        # SharedKeycloakIAMManager.run() stores the real client secret in a K8s
        # Secret named "{client_id}-client-secret". Read it back so the Helm
        # chart gets the correct OIDCClientSecret — not the placeholder from
        # secrets.yaml. This must happen BEFORE values() is called for Helm install.
        for iam in self._iter_iam_managers():
            if any(c.id == "keystone" for c in iam.cfg.clients):
                self._oidc_client_secret = iam.read_client_secret(kubectl, client_id="keystone")
                log.debug("[keystone] Keycloak client secret loaded from K8s ✓")
                break

        log.debug("[keystone] pre-install complete ✓")




    def _wait_for_init_jobs(self, kubectl):
        """Wait for critical Helm hook jobs to complete before proceeding."""
        critical_jobs = [
            "keystone-db-init",
            "keystone-db-sync",
            "keystone-rabbit-init",
            "keystone-fernet-setup",
            "keystone-credential-setup",
            "keystone-bootstrap",
        ]
        for job_name in critical_jobs:
            rc, _, _ = kubectl.run(
                ["get", "job", job_name, "-n", self.namespace, "-o", "name"],
            )
            if rc != 0:
                log.debug("[keystone] Job '%s' not found — skipping wait", job_name)
                continue

            log.info("[keystone] Waiting for job '%s' to complete...", job_name)
            kubectl.run(
                [
                    "wait", "--for=condition=complete",
                    f"job/{job_name}", "-n", self.namespace,
                    "--timeout=600s",
                ]
            )
            log.info("[keystone] Job '%s' completed", job_name)

    def post_install(self, kubectl):
        log.info("[keystone] Starting post-install...")
        self.kubectl = kubectl

        # Wait for Helm hook jobs (db-init, db-sync, etc.) to finish first
        log.info("[keystone] Waiting for init jobs to complete...")
        self._wait_for_init_jobs(kubectl)
        log.info("[keystone] Init jobs complete")

        # Parent handles Istio + validation
        log.info("[keystone] Configuring Istio virtual service...")
        super().post_install(kubectl)
        log.info("[keystone] Istio virtual service configured")

        log.info("[keystone] Waiting for keystone-api to be ready...")
        self._wait_for_keystone_ready(kubectl)
        log.info("[keystone] keystone-api is ready")

        log.info("[keystone] Ensuring Keycloak client for Keystone...")
        self._ensure_iam()
        self._create_keycloak_client()
        log.info("[keystone] Keycloak client configured")

        log.info("[keystone] Creating Keystone domains...")
        self._create_keystone_domains()
        log.info("[keystone] Keystone domains created")

        log.info("[keystone] Creating identity providers...")
        self._create_identity_providers()
        log.info("[keystone] Identity providers created")

        log.info("[keystone] Creating federated group and project...")
        self._create_federated_group_and_project()
        log.info("[keystone] Federated group and project ready")

        log.info("[keystone] Creating federation mappings...")
        self._create_federation_mappings()
        log.info("[keystone] Federation mappings created")

        log.info("[keystone] Creating federation protocols...")
        self._create_federation_protocols()
        log.info("[keystone] Federation protocols created")

        log.info("[keystone] Syncing shadow users into federated-users group...")
        self._sync_shadow_users_to_group()
        log.info("[keystone] Shadow user sync complete")

        log.info("[keystone] Deploying shadow-user sync CronJob...")
        self._deploy_shadow_user_sync_cronjob(kubectl)
        log.info("[keystone] Shadow-user sync CronJob deployed")

        # Permanent hairpin-NAT fix: inject hostAliases into the keystone-api
        # Deployment so auth.daalu.io always resolves to the Istio gateway IP
        # directly from /etc/hosts, independent of CoreDNS state.
        log.info("[keystone] Injecting hostAliases for permanent Keycloak DNS resolution...")
        self._inject_host_aliases(kubectl)
        log.info("[keystone] hostAliases injection complete")

        log.info("[keystone] Post-install complete")

    def _inject_host_aliases(self, kubectl) -> None:
        """
        Patch the keystone-api Deployment to add hostAliases that map the
        Keycloak hostname (e.g. auth.daalu.io) to the Istio ingress gateway's
        internal LoadBalancer IP.

        WHY THIS IS NEEDED
        ------------------
        mod_auth_openidc in Apache fetches the Keycloak OIDC discovery document
        at WebSSO request time:
            https://auth.daalu.io/realms/daalu/.well-known/openid-configuration

        Without this fix, `auth.daalu.io` resolves (via CoreDNS → public DNS) to
        the pfSense WAN IP. pfSense serves its own management UI on port 443 with
        a self-signed certificate. mod_auth_openidc rejects it and returns HTTP 500.

        PERMANENT NATURE OF THIS FIX
        -----------------------------
        hostAliases are part of the pod spec in the Deployment object (stored in
        etcd). They are:
        - Never reset by CoreDNS ConfigMap changes or Kubernetes upgrades
        - Written into every pod's /etc/hosts at container start
        - Reapplied on every `daalu openstack` run via this post_install method
        - Completely independent of CoreDNS (bypasses DNS lookup entirely)

        The CoreDNS split-horizon patch (kube_coredns.py) remains for other pods
        that also need to resolve cluster-hosted FQDNs internally, but Keystone
        no longer depends on it.
        """
        from urllib.parse import urlparse

        base_url = self.keycloak_config.admin.base_url  # e.g. "https://auth.daalu.io"
        keycloak_host = urlparse(str(base_url)).hostname
        if not keycloak_host:
            log.warning(
                "[keystone] Could not parse Keycloak hostname from %s; "
                "skipping hostAliases patch",
                base_url,
            )
            return

        rc, out, _ = kubectl._run(
            "get svc istio-ingressgateway -n istio-ingress"
            " -o jsonpath='{.status.loadBalancer.ingress[0].ip}'"
        )
        gateway_ip = out.strip().strip("'\"")
        if rc != 0 or not gateway_ip:
            log.warning(
                "[keystone] Could not determine Istio gateway IP; "
                "skipping hostAliases patch"
            )
            return

        log.info(
            "[keystone] Patching keystone-api Deployment: %s → %s",
            keycloak_host, gateway_ip,
        )
        kubectl.patch(
            api_version="apps/v1",
            kind="Deployment",
            name="keystone-api",
            namespace=self.namespace,
            patch={
                "spec": {
                    "template": {
                        "spec": {
                            "hostAliases": [
                                {"ip": gateway_ip, "hostnames": [keycloak_host]},
                            ]
                        }
                    }
                }
            },
        )

        log.info("[keystone] Waiting for keystone-api rollout with new hostAliases...")
        kubectl.wait_for_deployment_ready(
            name="keystone-api",
            namespace=self.namespace,
            timeout=300,
        )
        log.info(
            "[keystone] keystone-api pods now resolve %s → %s via /etc/hosts ✓",
            keycloak_host, gateway_ip,
        )

    # ----------------------------
    # Helper methods
    # ----------------------------
    def _iter_domains(self):
        return self.keycloak_config.normalized_domains()



    def _iter_iam_managers(self):
        """
        One IAM manager per Keycloak realm.
        """
        for domain in self._iter_domains():
            yield SharedKeycloakIAMManager(
                KeycloakIAMConfig(
                    k8s_namespace=self.keycloak_config.k8s_namespace,
                    admin=self.keycloak_config.admin,
                    realm=KeycloakRealmSpec(
                        realm=domain.keycloak_realm,
                        display_name=domain.label,
                    ),
                    clients=[domain.client] if domain.client else [],
                    oidc_issuer_url=f"{str(self.keycloak_config.admin.base_url).rstrip('/')}/realms/{domain.keycloak_realm}",
                    oauth2_proxy_ssl_insecure_skip_verify=
                        self.keycloak_config.oauth2_proxy_ssl_insecure_skip_verify,
                )
            )
