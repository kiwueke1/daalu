=============
Release Notes
=============

.. _Release Notes_v4.5.0:

v4.5.0
======

.. _Release Notes_v4.5.0_New Features:

New Features
------------

.. releasenotes/notes/Add-valkey-support-a125acc2495b731d.yaml @ b'bc4d79c53d5c699dbc3cd78ce615e65adac0b443'

- Valkey service is now available on Atmosphere.
  This is required service for introduce Octavia Amphora V2 support.

.. releasenotes/notes/add-amphere-v2-df19890ea67e2486.yaml @ b'04a2b2e197f5e726ea0b2700112bb688e9b50a24'

- Octavia Amphora V2 is now supported and enable by default with Atmosphere.
  The Amphora V2 provider driver improves control plane resiliency.
  Should a control plane host go down during a load balancer provisioning
  operation, an alternate controller can resume the in-process provisioning
  and complete the request. This solves the issue with resources stuck in
  PENDING_* states by writing info about task states in persistent data
  structure and monitoring job claims via Jobboard.

.. releasenotes/notes/lb-adopt-openstack-db-exporter-7eb5acc04769f007.yaml @ b'3bf94c47c6eba547097848f90d5b063e04397a0e'

- The OpenStack database exporter has been updated and the collection of Octavia metrics happens through it only.

.. releasenotes/notes/lb-adopt-openstack-db-exporter-7eb5acc04769f007.yaml @ b'3bf94c47c6eba547097848f90d5b063e04397a0e'

- Added alerting for amphoras to cover cases for when an Amphora becomes in ``ERROR`` state or not ready for an unexpected duration.

.. releasenotes/notes/update-frr-k8s-webhook-server-runs-on-control-plane-bcd5ef333c565f1b.yaml @ b'5e488f65118e1067c9c91a043627f12b31f7f0ce'

- Update the frr-k8s webhook server runs on the control plane.


.. _Release Notes_v4.5.0_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/backport-octavia-redis-fixes-551c9f3f145e3de6.yaml @ b'760a20bed78e149915b899283be017a3d38473f7'

- Backport fixes for Octavia Redis driver for support authentication
  and SSL for Redis Sentinel.

.. releasenotes/notes/fix-nova-resize-issue-c5a16a80056a2420.yaml @ b'149d98bbcf45271cbec6bfcc8df0234556b8c343'

- Addressed an issue where instances not booted from volume would fail to resize. This issue was caused by a missing trailing newline in the SSH key, which led to misinterpretation of the key material during the resize operation. Adding proper handling of SSH keys ensures that the resize process works as intended for all instances.


.. _Release Notes_v4.4.0:

v4.4.0
======

.. _Release Notes_v4.4.0_New Features:

New Features
------------

.. releasenotes/notes/add-frr-k8s-support-8cbfbff3e9c8d22f.yaml @ b'821de8f726e89907f02661559deaad8e0cddc270'

- Added support for deploying the frr-k8s chart for BGP routing with
  OVN. Introduced the ``ovn_bgp_agent_enabled`` flag. When set to
  ``true``, the frr-k8s chart will be automatically installed before
  OVN deployment.

.. releasenotes/notes/add-keycloak-token-exchange-283b38032dda9baf.yaml @ b'6b84c3e3254c67662c6835caa903cfbb7c729432'

- Keycloak is now configured to have the ``token-exchange`` and the ``admin-fine-grained-authz`` features enabled to allow for use of the `OAuth Token Exchange <https://www.keycloak.org/securing-apps/token-exchange>`_ protocol.

.. releasenotes/notes/add-ovn-bgp-agent-6cde4cb107ff2a3b.yaml @ b'cdd644ed6d1e413a4697ed478f76648331ed6679'

- The ``ovn-bgp-agent`` has been added to the chart. The ``ovn-bgp-agent``
  is deployed as a DaemonSet within the OVN Helm chart.

.. releasenotes/notes/add-ovn-bgp-agent-image-build-1dfc029599aacdcf.yaml @ b'81be5df4b44f9014c48348f5dfa3b513383aaa9e'

- Add OVN BGP Agent image build.


.. _Release Notes_v4.4.0_Security Issues:

Security Issues
---------------

.. releasenotes/notes/update-nginx-ingress-for-cve-6c2aea8e2c530421.yaml @ b'e221aa84fb4e0a64b8c1d92d2e3f3f29862d5b0b'

- Upgrade nginx ingress controller from 1.10.1 to 1.12.1 to fix CVE-2025-1097
  CVE-2025-1098, CVE-2025-1974, CVE-2025-24513, CVE-2025-24514.


.. _Release Notes_v4.4.0_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/bump-mcapi-75db7cc58ba546d2.yaml @ b'24bdab4fc066a083ae98137baf6d393e0694f427'

- The Cluster API driver for Magnum has been bumped to 0.28.0 to improve stability, fix bugs and add new features.

.. releasenotes/notes/fix-ironic-valid-interface-4abe135a9ff5b38e.yaml @ b'a105ceed2c6afecb2303c4f5f0f3bff4ddc9977e'

- The Ironic agent for Neutron uses the ``internal`` API endpoint by default to avoid hitting the public endpoint unnecessarily.

.. releasenotes/notes/fix-openstack-alert-OctaviaLoadBalancerNotActive-68315e228eecf7e3.yaml @ b'eb4365ba8a12e4de5f3426f7bbb872a52f2e2fc9'

- Improve alert generation for load balancers that have a non-``ACTIVE`` provisioning state
  despite an ``ONLINE`` operational state.  Previously, if a load balancer was in a
  transitional state such as ``PENDING_UPDATE`` (``provisioning_state``) while still marked
  as ``ONLINE`` (``operational_state``), the gauge metric
  ``openstack_loadbalancer_loadbalancer_status{provisioning_status!="ACTIVE"}`` did not
  trigger an alert.  This update addresses the issue by ensuring that alerts are properly
  generated in these scenarios.


.. _Release Notes_v4.3.1:

v4.3.1
======

.. _Release Notes_v4.3.1_New Features:

New Features
------------

.. releasenotes/notes/adding-nicname-as-an-option-f7e790ea8174e6af.yaml @ b'882a05046673f7a86488255cfc6e118fc9782adb'

- It is now possible to configure DPDK interfaces using the interface names in addition to
  possibly being able to use the ``pci_id`` to ease deploying in heterogeneous environments.


.. _Release Notes_v4.3.1_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/bump-mcapi-b1b3ac5df67ee216.yaml @ b'792aa6b6c47426080ba50d10e9bace778a6320ce'

- The Cluster API driver for Magnum has been bumped to 0.27.0 to improve stability, fix bugs and add new features.


.. _Release Notes_v4.3.0:

v4.3.0
======

.. _Release Notes_v4.3.0_New Features:

New Features
------------

.. releasenotes/notes/add-extra-keycloak-realm-options-a8b14740bd999ebb.yaml @ b'abfdb86e22058b09bd50a74e8a1eb763fd427c84'

- The Keystone role now supports additional parameters when creating the Keycloak realm to allow for the configuration of options such as password policy, brute force protection, and more.

.. releasenotes/notes/add-glance-image-tempfile-path-6c1ec42dccba948a.yaml @ b'e7e468beebb871bd2f0a38c3d57269bd544d364c'

- Add ``glance_image_tempfile_path`` variable to allow users for changing the temporary path for downloading images before uploading them to Glance.

.. releasenotes/notes/add-mfa-config-options-6f2d6811bca1a789.yaml @ b'72e21ddf0eb58b429c5cf4a77049aa059a202bd1'

- The Keystone role now supports configuring multi-factor authentication for the users within the Atmosphere realm.

.. releasenotes/notes/add-ovsinit-56990eaaf93c6f9d.yaml @ b'a7f726d54957bb8671df50974ff5c0fba40e68fc'

- Introduced a new Rust-based binary ``ovsinit`` which focuses on handling the migration of IP addresses from a physical interface to an OVS bridge during the Neutron or OVN initialization process.

.. releasenotes/notes/allow-configuring-ingress-class-name-0c50f395d9a1b213.yaml @ b'3aaaad747701e4cc5f32011d117d22f7b18733d5'

- All roles that deploy ``Ingress`` resources as part of the deployment
  process now support the ability to specify the class name to use for the
  ``Ingress`` resource.  This is done by setting the
  ``<role>_ingress_class_name`` variable to the desired class name.

.. releasenotes/notes/allow-using-default-cert-b28067c8a1525e1f.yaml @ b'c4ab95b7e9adbc8e5a6c1b1388cf7af8ad49c9cc'

- It's now possible to use the default TLS certificates configured within the
  ingress by using the ``ingress_use_default_tls_certificate`` variable which
  will omit the ``tls`` section from any ``Ingress`` resources managed by
  Atmosphere.

.. releasenotes/notes/barbican-priority-runtime-class-b84c8515f03e18c5.yaml @ b'edcbf1015843e8b9bdd7eb013002c10ed575e0d7'

- The Barbican role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/bump-storpool-caracal-525bae827bef1f62.yaml @ b'9f0a7008114c1d7bb0539bf1712ab19151820c67'

- The Storpool driver has been updated from the Bobcat release to the Caracal release.

.. releasenotes/notes/cinder-priority-runtime-class-910112b1da7bd5c1.yaml @ b'efaf37bfae5cfb6fa6f87bffbbddb43462e5323b'

- The Cinder role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/designate-priority-runtime-class-63f9e7efe1b3e494.yaml @ b'3e9431dfa7acc1b5f387466d813ab0eb3e7230bc'

- The Designate role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/enable-ovn-affinity-rules-54efa650be79426c.yaml @ b'97caeb2f5c63a0f4bb4283cef821080118b75bfd'

- Applied the same pod affinity rules used for OVN NB/SB sts's to northd deployment and
  changed the default pod affinity rules from preferred during scheduling to required
  during scheduling.

.. releasenotes/notes/glance-priority-runtime-class-8902ce859fba65f6.yaml @ b'1384adcb58b64b8997d58dc95df9feee81a397ae'

- The Glance role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/heat-priority-runtime-class-493ffeb8be07ac6a.yaml @ b'64a85d3d044c98e2f6151c3382d5d97656621afe'

- The Heat role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/horizon-priority-runtime-class-0004e6be3fdeab2b.yaml @ b'ceb22df1143b3e3d79e9fbfd8ab6a4015a3d227e'

- The Horizon role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/ironic-priority-runtime-class-260a89c958179e92.yaml @ b'00fb45b01e1723b0c873dea50bca46e71ec0de9b'

- The Ironic role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/keystone-priority-runtime-class-3d41226e8815f369.yaml @ b'2343e0f85e4e58c3725ec8b6d9651167a31907ea'

- The Keystone role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/magnum-priority-runtime-class-1fa01f838854cb94.yaml @ b'16a30bfeea3bbfa13ea251887f8c3145a6bf9ecb'

- The Magnum role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/manila-priority-runtime-class-2b73aa2ad577d258.yaml @ b'41aad17c5bf74005a0e926dece7d6eec441b7abb'

- The Manila role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/neutron-priority-runtime-class-b23c083ebd115e08.yaml @ b'56e2347446391bfd699f20699e6e06b784cd435b'

- The Neutron role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/nova-priority-runtime-class-97013402a7abf251.yaml @ b'68cb6c6f0d8c1430a97837db621e2543a4b4d37f'

- The Nova role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/octavia-priority-runtime-class-3803f91e26a627a4.yaml @ b'e8d8c62eed12a849e9fd2601addb0c20a44d9816'

- The Octavia role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/placement-priority-runtime-class-3d5598c95c26dc32.yaml @ b'06896b5cdc9be50fc95c393470c901b383d60077'

- The Placement role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.

.. releasenotes/notes/staffeln-priority-runtime-class-d7a4ae951ddcc214.yaml @ b'd3fcbe300e0eda314b45b984be107efc66fb3d0b'

- The Staffeln role now allows users to configure the ``priorityClassName`` and the ``runtimeClassName`` for all of the different components of the service.


.. _Release Notes_v4.3.0_Known Issues:

Known Issues
------------

.. releasenotes/notes/fix-ovn-mtu-d33352771a65e744.yaml @ b'6e4511805b634b04f33e05c0fccbfdac73d6e3d2'

- The MTU for the metadata interfaces for OVN was not being set correctly, leading to a mismatch between the MTU of the metadata interface and the MTU of the network.  This has been fixed with a Neutron change to ensure the ``neutron:mtu`` value in ``external_ids`` is set correctly.


.. _Release Notes_v4.3.0_Upgrade Notes:

Upgrade Notes
-------------

.. releasenotes/notes/magnum-update-mcapi-to-0.25.1-fbf7f3dd8b81489c.yaml @ b'0d70e36fc6d3b49524a1a693408a6b5a5d67b9ab'

- Upgrade Cluster API driver for Magnum to 0.26.0.


.. _Release Notes_v4.3.0_Security Issues:

Security Issues
---------------

.. releasenotes/notes/horizon-security-improvements-22b2535a85daab75.yaml @ b'290a75c7dd86f5687ece0ff28dd49400f81bba64'

- The Horizon service now runs as the non-privileged user `horizon` in the container.

.. releasenotes/notes/horizon-security-improvements-22b2535a85daab75.yaml @ b'290a75c7dd86f5687ece0ff28dd49400f81bba64'

- The Horizon service ``ALLOWED_HOSTS`` setting is now configured to point to the configured endpoints for the service.

.. releasenotes/notes/horizon-security-improvements-22b2535a85daab75.yaml @ b'290a75c7dd86f5687ece0ff28dd49400f81bba64'

- The CORS headers are now configured to only allow requests from the configured endpoints for the service.


.. _Release Notes_v4.3.0_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/add-ovsinit-56990eaaf93c6f9d.yaml @ b'a7f726d54957bb8671df50974ff5c0fba40e68fc'

- During a Neutron or OVN initialization process, the routes assigned to the physical interface are now removed and added to the OVS bridge to maintain the connectivity of the host.

.. releasenotes/notes/bump-mcapi-bde5d8909e7f6268.yaml @ b'68d2ce31dd0666e31c117c87c9b27753ac350995'

- The Cluster API driver for Magnum has been bumped to 0.26.2 to address bugs around cluster deletion.

.. releasenotes/notes/fix-manila-device-name-mixed-2cb5f82275df359c.yaml @ b'2e2bd688e6ebc3c39c096bb7b91b8ae9c4feaa49'

- Updated Manila to utilize device UUIDs instead of device names for mounting
  operations. This change ensures consistent device identification and
  prevents device name conflicts that could occur after rebooting the Manila
  server.

.. releasenotes/notes/fix-two-redundant-securityContext-problems-28bfb724627e8920.yaml @ b'd7aa6de1ddd797432b90a393a9f2444c0651898c'

- Fix two redundant securityContext problems in
  statefulset-compute-ironic.yaml template.


.. _Release Notes_v4.3.0_Other Notes:

Other Notes
-----------

.. releasenotes/notes/bump-openstack-collection-382923f617548b01.yaml @ b'5bd234dd7342b26ea35f923e5ad3e3988225ca2a'

- The Atmosphere collection now uses the new major version of the OpenStack collection as a dependency.


.. _Release Notes_v4.2.12:

v4.2.12
=======

.. _Release Notes_v4.2.12_New Features:

New Features
------------

.. releasenotes/notes/enable-ovn-northd-liveness-probe-8b80c6e4399c5225.yaml @ b'd94bb7e200a3c3c164dd45d582427b79888eb1a0'

- The ``ovn-northd`` service did not have liveness probes enabled which can result in the pod failing readiness checks but not being automatically restarted.  The liveness probe is now enabled by default which will restart any stuck ``ovn-northd`` processes.

.. releasenotes/notes/ovn-dhcp-agent-6da645f88a2c39c3.yaml @ b'f1cf7ef7d3ed27a04a7d711160608b6d7674d539'

- Neutron now supports using the built-in DHCP agent when using OVN (Open Virtual Network)
  for cases when DHCP relay is necessary.


.. _Release Notes_v4.2.12_Upgrade Notes:

Upgrade Notes
-------------

.. releasenotes/notes/bump-ovn-version-d4216ca44d5e6f41.yaml @ b'02db0cc964ad6f5d19701598c09c0a2b006cad3f'

- Bump OVN from 24.03.1-44 to 24.03.2.34.


.. _Release Notes_v4.2.12_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/add-missing-osbrick-helper-0bc348399986a5d6.yaml @ b'c806af1eaef00e052737dc8eca25f598dc6c55a5'

- The ``[privsep_osbrick]/helper_command`` configuration value was not configured in both of the Cinder and Nova services, which lead to the inability to run certain CLI commands since it instead tried to do a plain ``sudo`` instead.  This has been fixed by adding the missing helper command configuration to both services.

.. releasenotes/notes/add-missing-osbrick-helper-0bc348399986a5d6.yaml @ b'c806af1eaef00e052737dc8eca25f598dc6c55a5'

- The ``dmidecode`` package which is required by the ``os-brick`` library for certain operations was not installed on the images that needed it, which can cause NVMe-oF discovery issues.  The package has been added to all images that require it.

.. releasenotes/notes/add-missing-shell-dc5f8d4fca30eca6.yaml @ b'8e269fe32a19ff46b10c4428076d06de70e38eb4'

- The ``nova`` user within the ``nova-ssh`` image was missing the ``SHELL`` build argument which would cause live & cold migrations to fail, this has been resolved by adding the missing build argument.

.. releasenotes/notes/fix-aio-max-limit-228f73927b88d3ee.yaml @ b'baab510bc2186688ca65f0156f025bd3b6679306'

- This fix introduces a kernel option to adjust ``aio-max-nr``, ensuring that the
  system can handle more asynchronous I/O events, preventing VM startup
  failures related to AIO limits.

.. releasenotes/notes/use-internal-endpoint-for-magnum-capi-client-in-default-da61531ce88c94aa.yaml @ b'02ac5941bd3d208e6963b1a76ba825c639654935'

- The Cluster API driver for Magnum is now configured to use the internal
  endpoints by default in order to avoid going through the ingress and
  leverage client-side load balancing.


.. _Release Notes_v4.2.11:

v4.2.11
=======

.. _Release Notes_v4.2.11_New Features:

New Features
------------

.. releasenotes/notes/add-0.2.78-specific-helm-toolkit-91bafe001411ae38.yaml @ b'cd50582c147f8f5f2171f10e3aacbf7db55656c5'

- Add specific helm-toolkit patch on 0.2.78. This will allow DB drop and init job
  compatible with SQLAlchemy 2.0


.. _Release Notes_v4.2.11_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/bump-openvswitch-435cea61eec39371.yaml @ b'4e9ef3b3a8a0379aa4ea08237bc5d9185c985a7d'

- The Open vSwitch version has been bumped to 3.3.0 in order to resolve packet drops include ``Packet dropped. Max recirculation depth exceeded.`` log messages in the Open vSwitch log.


.. _Release Notes_v4.2.11_Other Notes:

Other Notes
-----------

.. releasenotes/notes/use-docker-bake-526459f34fabc32b.yaml @ b'cc6868be4329260260e05b8b317b3358d88a378d'

- The image build process has been refactored to use ``docker-bake`` which allows us to use context/built images from one target to another, allowing for a much easier local building experience.  There is no functional change in the images.


.. _Release Notes_v4.2.10:

v4.2.10
=======

.. _Release Notes_v4.2.10_New Features:

New Features
------------

.. releasenotes/notes/add-neutron-policy-server-address-pair-193aa1434c376c10.yaml @ b'4ba7bdbf0814b669c3c7a606d0154113b8c95828'

- Add support for Neutron policy check when perform port update with
  add address pairs. This will add a POST method ``/address-pair``.
  It will check if both ports (to be paired) are created within same project.
  With this check, we can give non-admin user to operate address pair binding
  without risk on expose resource to other projects.

.. releasenotes/notes/allow-prefix-to-image-names-4a795e9ff805b8b0.yaml @ b'5423812245e60014746dc5b4d117e547c484fd29'

- Introduced the ability to specify a prefix for image names. This allows for
  easier integration with image proxies and caching mechanisms, eliminating
  the need to maintain separate inventory overrides for each image.

.. releasenotes/notes/prepull-ovn-controller-62f8a216e8b41c9f.yaml @ b'8d059c21482a62c7077c0aa15fb6a54edcde3971'

- The ``ovn-controller`` image is now being pre-pulled on the nodes prior to the Helm chart being deployed.  This will help reduce the time it takes to switch over to the new version of the ``ovn-controller`` image.


.. _Release Notes_v4.2.10_Bug Fixes:

Bug Fixes
---------

.. releasenotes/notes/fix-neutron-ironic-agent-f3eedbcec84b0478.yaml @ b'cd00c3d9ef656fe10f2db30846d0e802edfcc686'

- Fixed an issue where the ``neutron-ironic-agent`` service failed to start.

.. releasenotes/notes/fix-ovs-dpdk-permission-issue-fea15d01685d2e1b.yaml @ b'bb492dfe8d44beda628c6ba61c2b45217195d497'

- When use OVS with DPDK, by default both OVS and OVN run with root user, this
  may cause issue that QEMU can't write vhost user socket file in openvswitch
  runtime directory (``/run/openvswitch``). This has been fixed by config Open
  vSwitch and OVN componments to run with non root user id 42424 which is same
  with QEMU and other OpenStack services inside the container.

.. releasenotes/notes/fix-pin-images-473f8e1cf4a81afc.yaml @ b'1cfa0ce745d9b93db8602b5b9a2080d52665e8a5'

- The CI tooling for pinning images has been fixed to properly work after a regression caused by the introduction of the ``atmosphere_image_prefix`` variable.

.. releasenotes/notes/fix-tpm-docs-d4cc722764f61032.yaml @ b'4de00288dfdb7fd1f445f518be2f35289b0b82ad'

- The documentation for using the vTPM was pointing to the incorrect metadata properties for images.  This has been corrected to point to the correct metadata properties.


.. _Release Notes_v4.2.10_Other Notes:

Other Notes
-----------

.. releasenotes/notes/enforce-release-notes-31a9388c10d21d53.yaml @ b'64eba867d13d7193175167648fb333641f2f979b'

- The project has adopted the use of ``reno`` for release notes, ensuring that all changes include it from now on to ensure proper release notes.

.. releasenotes/notes/skip-releasenotes-acb170de807b80bc.yaml @ b'57bd9870fe42522cf3a86ceb7c540584f56b8509'

- The heavy CI jobs are now skipped when release notes are changed.

