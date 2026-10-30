# Acronyms & Glossary

A reference for the acronyms, initialisms, and shorthand used throughout the
`docs/` tree. Definitions are written for *this* project — where an acronym has a
generic meaning but a specific role in Daalu, the Daalu context is called out.

Entries are alphabetical. Numbers and model names (e.g. GPU SKUs like `A100`,
`H100`) are grouped at the end under **Hardware model names**.

---

## A

- **ABC** — Abstract Base Class. Python's `abc` module; used in the Source-of-Truth layer as a test seam (the Nautobot adapter implements an ABC interface).
- **ACL** — Access Control List.
- **ACME** — Automated Certificate Management Environment. The protocol cert-manager uses to obtain TLS certificates (e.g. from Let's Encrypt).
- **AD** — Active Directory. Microsoft's directory service; "Azure AD" (now Entra ID) is referenced for SSO/identity federation.
- **AES** — Advanced Encryption Standard. Symmetric cipher; appears as `AES-256` and `AESGCM` (AES in Galois/Counter Mode) for secret encryption.
- **AI** — Artificial Intelligence.
- **AKS** — Azure Kubernetes Service.
- **AM** — Alertmanager (Prometheus component) — also "AM" the time designator in some tables.
- **AMI** — Amazon Machine Image (EC2 boot image).
- **API** — Application Programming Interface.
- **ARN** — Amazon Resource Name (globally unique AWS resource identifier).
- **ASG** — Auto Scaling Group (AWS EC2).
- **ASGI** — Asynchronous Server Gateway Interface. The async Python web server interface FastAPI runs on.
- **AWQ** — Activation-aware Weight Quantization. An LLM weight-quantization method; appears as `AWQ-INT4` (4-bit).
- **AWS** — Amazon Web Services.
- **AZ** — Availability Zone (cloud-provider fault domain).

## B

- **BAAI** — Beijing Academy of Artificial Intelligence. Producer of the BGE embedding models.
- **BF16** — Brain Floating Point 16-bit. A 16-bit numeric format for model weights/activations.
- **BFF** — Backend For Frontend. An API layer tailored to a specific frontend.
- **BGE** — BAAI General Embedding. Open embedding-model family used for RAG.
- **BGP** — Border Gateway Protocol (routing).
- **BIOS** — Basic Input/Output System (firmware).
- **BMC** — Baseboard Management Controller. Out-of-band server management (IPMI/Redfish) used in bare-metal provisioning.
- **BYO** — Bring Your Own. Prefix for customer-supplied resources: **BYO-GPU**, **BYO-NIC**.

## C

- **CA** — Certificate Authority.
- **CAPI** — Cluster API (Kubernetes cluster-lifecycle project).
- **CD** — Continuous Delivery / Continuous Deployment.
- **CDI** — Container Device Interface. How container runtimes expose GPUs to containers.
- **CDN** — Content Delivery Network.
- **CI** — Continuous Integration.
- **CIDR** — Classless Inter-Domain Routing (IP range notation, e.g. `10.0.0.0/8`).
- **CLI** — Command-Line Interface.
- **CM** — Config Manager. Shorthand for NVIDIA Config Manager (see **NV-CM**).
- **CMDB** — Configuration Management Database.
- **CNCF** — Cloud Native Computing Foundation.
- **CNI** — Container Network Interface (Kubernetes pod networking plugin).
- **CNPG** — CloudNativePG. The Kubernetes operator running PostgreSQL in-cluster.
- **CORS** — Cross-Origin Resource Sharing.
- **CPI** — Cycles Per Instruction. CPU performance metric derived from DCGM/PMU counters.
- **CPU** — Central Processing Unit.
- **CR** — Custom Resource (Kubernetes instance of a CRD).
- **CRD** — Custom Resource Definition (Kubernetes API extension).
- **CRI-O** — Container Runtime Interface for OCI. A lightweight Kubernetes container runtime.
- **CRIU** — Checkpoint/Restore In Userspace. Used for process/container snapshotting.
- **CRM** — Customer Relationship Management (e.g. Salesforce/HubSpot integrations).
- **CRUD** — Create, Read, Update, Delete.
- **CSI** — Container Storage Interface (Kubernetes storage plugin).
- **CSP** — Cloud Service Provider.
- **CSRF** — Cross-Site Request Forgery.
- **CSS** — Cascading Style Sheets.
- **CSV** — Comma-Separated Values.
- **CTA** — Call To Action (UI/marketing).
- **CUDA** — Compute Unified Device Architecture. NVIDIA's GPU compute platform.
- **CVE** — Common Vulnerabilities and Exposures (security advisory identifier).

## D

- **DB** — Database.
- **DBE** — Double-Bit Error. Uncorrectable ECC memory error on a GPU.
- **DCGM** — Data Center GPU Manager. NVIDIA's GPU monitoring/telemetry stack; `dcgm-exporter` feeds Prometheus.
- **DCIM** — Data Center Infrastructure Management (a Nautobot domain).
- **DEK** — Data Encryption Key (envelope-encryption inner key; wrapped by a KEK).
- **DGX** — NVIDIA's integrated data-center AI server line.
- **DHCP** — Dynamic Host Configuration Protocol.
- **DKMS** — Dynamic Kernel Module Support (rebuilds kernel modules like the NVIDIA driver across kernel upgrades).
- **DNS** — Domain Name System.
- **DNS-01 / HTTP-01** — ACME challenge types for proving domain control when issuing TLS certs.
- **DOCA** — NVIDIA's BlueField/DPU software framework.
- **DPO** — Direct Preference Optimization (LLM alignment method).
- **DPU** — Data Processing Unit (e.g. NVIDIA BlueField).
- **DR** — Disaster Recovery.
- **DRA** — Dynamic Resource Allocation. Kubernetes mechanism for flexible device (GPU) requests.
- **DRF** — Django REST Framework (Nautobot's API layer).
- **DSL** — Domain-Specific Language.

## E

- **E2E** — End-to-End (testing/verification).
- **EBS** — Elastic Block Store (AWS block storage).
- **EC2** — Elastic Compute Cloud (AWS VMs).
- **ECC** — Error-Correcting Code (memory). GPU ECC errors are SBE/DBE.
- **ECN** — Explicit Congestion Notification (RDMA/Ethernet).
- **ECS** — Elastic Container Service (AWS).
- **EKS** — Elastic Kubernetes Service (AWS).
- **EOS** — End Of Sequence. The token that signals generation should stop.
- **EULA** — End-User License Agreement.

## F

- **FAQ** — Frequently Asked Questions.
- **FK** — Foreign Key (database).
- **FOSS** — Free and Open-Source Software. A hard project constraint: serving uses FOSS (vLLM + open weights), no proprietary NVIDIA AI Enterprise entitlements.
- **FP4 / FP8 / FP16** — Floating Point at 4/8/16-bit precision. Lower precision = less VRAM, faster inference.

## G

- **GA** — General Availability (release maturity).
- **GB** — Gigabyte.
- **GCP** — Google Cloud Platform.
- **GCS** — Google Cloud Storage.
- **GDDR6 / GDDR7** — Graphics Double Data Rate memory (GPU VRAM type).
- **GDPR** — General Data Protection Regulation (EU privacy law).
- **GDS** — GPUDirect Storage (NVIDIA direct GPU↔storage data path).
- **GFD** — GPU Feature Discovery. Labels Kubernetes nodes with GPU capabilities; part of the GPU Operator.
- **GHCR** — GitHub Container Registry (`ghcr.io`).
- **GKE** — Google Kubernetes Engine.
- **GPT** — Generative Pre-trained Transformer.
- **GPU** — Graphics Processing Unit.
- **GRPO** — Group Relative Policy Optimization (RL fine-tuning method).
- **GSP** — GPU System Processor. On-GPU microcontroller; its firmware can hang under rapid device churn.
- **GUID** — Globally Unique Identifier.

## H

- **HA** — High Availability.
- **HBM** — High Bandwidth Memory (data-center GPU VRAM type).
- **HF** — Hugging Face (model/dataset hub).
- **HIPAA** — Health Insurance Portability and Accountability Act (US healthcare compliance).
- **HMAC** — Hash-based Message Authentication Code; appears as `HMAC-SHA512`.
- **HPC** — High-Performance Computing.
- **HS256 / RS256** — JWT signing algorithms (HMAC-SHA256 / RSA-SHA256).
- **HTML** — HyperText Markup Language.
- **HTTP / HTTPS** — HyperText Transfer Protocol (Secure).
- **HW** — Hardware.

## I

- **IAM** — Identity and Access Management.
- **IB** — InfiniBand (low-latency HPC/GPU interconnect).
- **ID** — Identifier.
- **IDE** — Integrated Development Environment.
- **IMEX** — Internode Memory Exchange (NVIDIA multi-node GPU memory sharing).
- **INT4 / INT8** — 4-bit / 8-bit Integer quantization.
- **IOS / IOS-XR** — Cisco network operating systems.
- **IP** — Internet Protocol.
- **IPAM** — IP Address Management (a Nautobot domain).
- **IPMI** — Intelligent Platform Management Interface (out-of-band server management).
- **ISP** — Internet Service Provider.
- **IT** — Information Technology.
- **ITL** — Inter-Token Latency. Time between successive generated tokens (LLM serving SLO).

## J

- **JS** — JavaScript.
- **JSON** — JavaScript Object Notation.
- **JSONB** — Binary JSON. PostgreSQL's indexed JSON column type.
- **JSONL** — JSON Lines (one JSON object per line).
- **JSON-RPC** — JSON Remote Procedure Call (used by MCP).
- **JWKS** — JSON Web Key Set. The public keys used to verify JWT signatures.
- **JWT** — JSON Web Token. Signed token carrying identity/claims for auth.

## K

- **KAI** — Kubernetes AI Scheduler. NVIDIA's open scheduler for fair-share, GPU fractioning, and gang scheduling.
- **KC** — Keycloak (the identity provider / SSO server).
- **KEK** — Key Encryption Key (envelope-encryption outer key; wraps DEKs).
- **KMS** — Key Management Service.
- **KV** — Key-Value (cache/store; in LLM context, the attention key/value cache).
- **KVBM** — KV Block Manager. NVIDIA Dynamo component that tiers/offloads the KV cache across memory hierarchies.
- **KVM** — Kernel-based Virtual Machine (Linux hypervisor).

## L

- **LAN** — Local Area Network.
- **LB** — Load Balancer.
- **LE** — Let's Encrypt (the ACME certificate authority).
- **LLC** — Last-Level Cache (CPU); appears in `LLC-MPKI`.
- **LLM** — Large Language Model.
- **LM** — Language Model.
- **LPDDR5X** — Low-Power DDR5X memory (e.g. on Grace/GB10 superchips).
- **LSP** — Language Server Protocol (powers the coding assistant's editor features).

## M

- **MAC** — Media Access Control address.
- **MCP** — Model Context Protocol. Standard for exposing tools/context to LLM agents.
- **MIG** — Multi-Instance GPU. Hardware partitioning of a GPU into isolated slices (not available on RTX 6000 Ada → soft isolation used instead).
- **MIT** — MIT License (permissive open-source license).
- **ML** — Machine Learning.
- **MOFED / OFED** — (Mellanox) OpenFabrics Enterprise Distribution. RDMA/InfiniBand drivers.
- **MOK** — Machine Owner Key. Secure Boot key used to sign the NVIDIA kernel module.
- **MPI** — Message Passing Interface (multi-node parallel computing).
- **MPKI** — Misses Per Kilo-Instruction (cache-performance metric).
- **MPS** — Multi-Process Service. NVIDIA mechanism for sharing a GPU across processes.
- **MSP** — Managed Service Provider.
- **MSRP** — Manufacturer's Suggested Retail Price.
- **MTU** — Maximum Transmission Unit (network packet size).
- **MVP** — Minimum Viable Product.

## N

- **NAPALM** — Network Automation and Programmability Abstraction Layer with Multivendor support.
- **NAT** — Network Address Translation.
- **NATS** — Cloud-native messaging system (used as an event/message bus).
- **NCCL** — NVIDIA Collective Communications Library (multi-GPU collectives).
- **NETCONF** — Network Configuration Protocol.
- **NFD** — Node Feature Discovery (labels Kubernetes nodes with hardware features).
- **NGC** — NVIDIA GPU Cloud (NVIDIA's container/model registry — *avoided* under the FOSS constraint).
- **NIC** — Network Interface Card.
- **NIM** — NVIDIA Inference Microservice (proprietary; avoided under the FOSS constraint).
- **NIXL** — NVIDIA Inference Xfer Library. Transfer library for disaggregated serving (pairs with Dynamo).
- **NPE** — Null Pointer Exception. (Referenced re: a broken Keycloak v1 token-exchange path on 26.5.1.)
- **NV-CM** — **NVIDIA Config Manager.** Network/config-management control plane Daalu drives per-tenant (alongside Nautobot) for device onboarding.
- **NVAIE** — NVIDIA AI Enterprise (proprietary suite; explicitly *not* used).
- **NVFP4** — NVIDIA 4-bit Floating Point format (Blackwell-era low-precision).
- **NVML** — NVIDIA Management Library (programmatic GPU telemetry/control).
- **NVOS / NVUE** — NVIDIA OS / NVIDIA User Experience API (Cumulus/Spectrum switches).
- **NVVS** — NVIDIA Validation Suite (GPU health/diagnostics).
- **NX-OS** — Cisco Nexus operating system.

## O

- **OCI** — Open Container Initiative (container image/runtime standards).
- **OFED** — OpenFabrics Enterprise Distribution (see **MOFED**).
- **OIDC** — OpenID Connect. The OAuth2-based identity layer used for SSO.
- **OOM** — Out Of Memory.
- **ONNX** — Open Neural Network Exchange (portable model format).
- **OPS** — Operations (also "ops.example.com", the operator console).
- **OQ** — Open Question. Tracked decision items in the plans docs (e.g. `OQ1`, `OQ2`).
- **OS** — Operating System.
- **OSS** — Open-Source Software.
- **OTEL** — OpenTelemetry (observability instrumentation standard).

## P

- **PAT** — Personal Access Token.
- **PCI / PCIe** — Peripheral Component Interconnect (Express). The bus GPUs/NICs attach to.
- **PDB** — Pod Disruption Budget (Kubernetes availability guard).
- **PFC** — Priority Flow Control (lossless Ethernet for RoCE).
- **PG** — PostgreSQL (also "Postgres").
- **PII** — Personally Identifiable Information.
- **PKCE** — Proof Key for Code Exchange. OAuth2 extension securing the auth-code flow (`S256` challenge method).
- **PKI** — Public Key Infrastructure.
- **PMU** — Performance Monitoring Unit (hardware counters).
- **PR** — Pull Request.
- **PVC** — Persistent Volume Claim (Kubernetes storage request).
- **PXE** — Preboot Execution Environment (network boot).

## R

- **RAG** — Retrieval-Augmented Generation. Grounding LLM answers in retrieved documents/embeddings.
- **RAID** — Redundant Array of Independent Disks.
- **RAM** — Random Access Memory.
- **RAPIDS** — NVIDIA's GPU-accelerated data-science libraries.
- **RBAC** — Role-Based Access Control.
- **RBD** — RADOS Block Device (Ceph block storage).
- **RCA** — Root Cause Analysis.
- **RDMA** — Remote Direct Memory Access. Zero-copy networking for GPU/HPC fabrics.
- **RDS** — Relational Database Service (AWS managed databases).
- **REST** — Representational State Transfer (API style).
- **RFC** — Request For Comments (standards document); `RFC1918` = private IP ranges.
- **RGW** — RADOS Gateway (Ceph S3/Swift object gateway).
- **RL** — Reinforcement Learning.
- **RLHF** — Reinforcement Learning from Human Feedback.
- **RLS** — Row-Level Security (PostgreSQL multi-tenant isolation).
- **RoCE** — RDMA over Converged Ethernet.
- **RP** — Relying Party (the OIDC client application).
- **RS256** — RSA-SHA256 JWT signing algorithm.
- **RTX** — NVIDIA's RTX (ray-tracing) GPU brand (e.g. RTX 6000 Ada, RTX 2000 Ada).
- **RWO** — ReadWriteOnce. PVC access mode (mountable by a single node).

## S

- **S3** — Simple Storage Service (AWS object storage; also the de-facto object-store API).
- **SA** — Service Account (Kubernetes/cloud machine identity).
- **SAML** — Security Assertion Markup Language (enterprise SSO standard).
- **SAN** — Subject Alternative Name (TLS certificate field).
- **SBE** — Single-Bit Error. Correctable ECC memory error.
- **SDK** — Software Development Kit.
- **SFT** — Supervised Fine-Tuning.
- **SHA** — Secure Hash Algorithm (e.g. `SHA-256`).
- **SKU** — Stock Keeping Unit. A billable product/resource line item (e.g. a GPU SKU).
- **SLA** — Service Level Agreement.
- **SLO** — Service Level Objective.
- **SM** — Streaming Multiprocessor (GPU compute unit).
- **SMTP** — Simple Mail Transfer Protocol (email).
- **SNS** — Simple Notification Service (AWS).
- **SOC 2** — Service Organization Control 2 (security/compliance audit).
- **SOPS** — Secrets OPerationS. Mozilla SOPS, used to encrypt secrets in Git.
- **SoT / SOT** — Source of Truth. The authoritative inventory/config layer (Nautobot-backed).
- **SP** — Service Principal (Azure AD machine identity).
- **SPA** — Single-Page Application.
- **SQL** — Structured Query Language.
- **SR-IOV** — Single-Root I/O Virtualization. Splits a NIC into virtual functions (VFs).
- **SRE** — Site Reliability Engineering.
- **SSD** — Solid-State Drive.
- **SSE** — Server-Sent Events (streaming HTTP, used for token streaming).
- **SSH** — Secure Shell.
- **SSO** — Single Sign-On.
- **STS** — Security Token Service (AWS temporary credentials).

## T

- **TB** — Terabyte.
- **TCO** — Total Cost of Ownership.
- **TCP** — Transmission Control Protocol.
- **TDP** — Thermal Design Power (a GPU/CPU's power/heat envelope in watts).
- **TEE** — Trusted Execution Environment (confidential computing).
- **TEI** — Text Embeddings Inference (Hugging Face embedding server).
- **TGI** — Text Generation Inference (Hugging Face LLM server).
- **TID** — Tenant ID.
- **TLS** — Transport Layer Security.
- **TPOT** — Time Per Output Token (LLM serving latency metric).
- **TRT-LLM** — TensorRT-LLM. NVIDIA's optimized LLM inference engine.
- **TSDB** — Time-Series Database (e.g. Prometheus).
- **TTFT** — Time To First Token. Latency from request to first generated token.
- **TTL** — Time To Live (cache/DNS expiry).
- **TUI** — Text User Interface.

## U

- **UCX** — Unified Communication X (high-performance communication framework under NIXL/NCCL).
- **UDP** — User Datagram Protocol.
- **UEFI** — Unified Extensible Firmware Interface (modern BIOS replacement).
- **UI** — User Interface.
- **URI / URL** — Uniform Resource Identifier / Locator.
- **UUID** — Universally Unique Identifier.
- **UX** — User Experience.

## V

- **VAT** — Value-Added Tax (billing).
- **VF** — Virtual Function (an SR-IOV slice of a NIC).
- **VLAN** — Virtual Local Area Network.
- **VM** — Virtual Machine.
- **VPC** — Virtual Private Cloud.
- **VPN** — Virtual Private Network.
- **VRAM** — Video RAM (GPU memory).
- **VRF** — Virtual Routing and Forwarding.

## W

- **WAN** — Wide Area Network.
- **WCAG** — Web Content Accessibility Guidelines.
- **WG** — WireGuard. The VPN tunnel binding edge clusters to the hub.
- **WS** — Work Stream. Plan/roadmap workstreams (`WS1`–`WS4`).

## X

- **XID / Xid** — NVIDIA GPU error code reported by the driver (diagnoses GPU faults).
- **XSS** — Cross-Site Scripting.

## Z

- **ZTP** — Zero-Touch Provisioning (hands-off network/server bring-up).

---

## Hardware model names

GPU and chip model designators that appear throughout the GPU/serving docs.

- **A100 / H100 / H200 / B100 / B200** — NVIDIA data-center GPUs (Ampere → Hopper → Blackwell generations).
- **A40 / L4 / L40S** — NVIDIA data-center/inference GPUs.
- **RTX 2000 Ada / RTX 6000 Ada** — NVIDIA professional (workstation) Ada-generation GPUs commonly used for local serving.
- **AD102** — The Ada Lovelace GPU die (e.g. in RTX 6000 Ada / L40S).
- **GB10** — NVIDIA Grace Blackwell superchip (Grace CPU + Blackwell GPU).
- **DGX** — NVIDIA's integrated data-center AI systems.

---

## Project-specific tools & code names

Not acronyms, but recurring proper nouns worth a one-line definition.

- **Daalu Edge** — The agent deployed on a customer cluster that joins it to the Daalu hub over WireGuard.
- **Dynamo** — NVIDIA's open disaggregated LLM-serving framework (`ai-dynamo`).
- **Grove** — NVIDIA Kubernetes orchestrator (`ai-dynamo/grove`) for multi-node gang-scheduled serving.
- **Nautobot** — The network Source-of-Truth / DCIM / IPAM platform Daalu integrates with.
- **NVSentinel** — NVIDIA's GPU fault auto-remediation controller (cordon/drain/reboot bad nodes).
- **TopoGraph** — Topology-aware placement service feeding the KAI scheduler for multi-node jobs.
- **vLLM** — The open-source LLM inference/serving engine Daalu standardizes on.
