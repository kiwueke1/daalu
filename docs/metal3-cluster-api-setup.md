# Metal3 + Cluster API Management Cluster Setup

This guide prepares a Kubernetes management cluster to provision bare-metal infrastructure using:

- Cluster API (CAPI)
- Metal3 Infrastructure Provider
- Ironic Standalone Operator
- Baremetal Operator (BMO)

This cluster will be used by Daalu to manage workload clusters.

---

## Prerequisites

- Kubernetes cluster (v1.26+ recommended)
- `kubectl`
- `clusterctl`

Verify:

```bash
kubectl version --client
clusterctl version
```

---

# 1️⃣ Install cert-manager

Metal3 components require cert-manager.

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

kubectl -n cert-manager rollout status deploy/cert-manager --timeout=5m
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=5m
kubectl -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=5m
```

---

# 2️⃣ Install Cluster API Core

```bash
clusterctl init \
  --core cluster-api:v1.12.0 \
  --bootstrap kubeadm:v1.12.0 \
  --control-plane kubeadm:v1.12.0
```

Verify:

```bash
kubectl get pods -n capi-system
kubectl get pods -n capi-kubeadm-bootstrap-system
kubectl get pods -n capi-kubeadm-control-plane-system
```

---

# 3️⃣ Install Ironic Standalone Operator

```bash
kubectl apply -f https://github.com/metal3-io/ironic-standalone-operator/releases/latest/download/install.yaml

kubectl -n ironic-standalone-operator-system wait \
  --for=condition=Available \
  deploy/ironic-standalone-operator-controller-manager --timeout=5m
```

---

# 4️⃣ Create Metal3 Namespace

```bash
kubectl create ns baremetal-operator-system
```

---

# 5️⃣ Deploy Ironic Instance

Deploy your Ironic custom resource (example: `daalu-ironic`).

Apply your kustomize manifest:

```bash
kubectl apply -k .
```

Wait for Ironic:

```bash
kubectl -n baremetal-operator-system wait \
  --for=condition=Ready ironic/daalu-ironic --timeout=10m
```

---

# 6️⃣ Retrieve Ironic Credentials

```bash
NS=baremetal-operator-system

SECRET=$(kubectl get ironic/daalu-ironic -n $NS -o jsonpath='{.spec.apiCredentialsName}')

USERNAME=$(kubectl get secret/$SECRET -n $NS -o jsonpath='{.data.username}' | base64 -d)
PASSWORD=$(kubectl get secret/$SECRET -n $NS -o jsonpath='{.data.password}' | base64 -d)

kubectl -n $NS get secret daalu-ironic-cacert \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > ironic-ca.crt
```

---

# 7️⃣ Install Baremetal Operator (BMO)

Create authentication secret:

```bash
kubectl -n $NS create secret generic daalu-ironic-auth \
  --from-literal=username="$USERNAME" \
  --from-literal=password="$PASSWORD" \
  --from-file=cacert=./ironic-ca.crt
```

Install BMO:

```bash
git clone https://github.com/metal3-io/baremetal-operator.git
cd baremetal-operator
kubectl apply -k config/default
```

Patch BMO to connect to Ironic:

```bash
kubectl -n baremetal-operator-system set env deploy/baremetal-operator-controller-manager \
  IRONIC_ENDPOINT="https://daalu-ironic.baremetal-operator-system.svc.cluster.local:6385" \
  METAL3_AUTH_ROOT_DIR="/opt/metal3/auth" \
  IRONIC_CACERT_FILE="/opt/metal3/auth/ironic/cacert"
```

Mount credentials:

```bash
kubectl -n baremetal-operator-system patch deploy/baremetal-operator-controller-manager --type='strategic' -p '
spec:
  template:
    spec:
      volumes:
      - name: ironic-auth
        secret:
          secretName: daalu-ironic-auth
      containers:
      - name: manager
        volumeMounts:
        - name: ironic-auth
          mountPath: /opt/metal3/auth/ironic
          readOnly: true
'
```

Verify:

```bash
kubectl get pods -n baremetal-operator-system
```

---

# 8️⃣ Install Cluster API Provider Metal3

```bash
clusterctl init --infrastructure metal3:v1.12.1
```

(Optional IPAM provider)

```bash
clusterctl init --ipam metal3
```

Verify:

```bash
kubectl get pods -n capi-metal3-system
```

---

# 9️⃣ Create a BareMetalHost (BMH)

Now register your physical server.

## Create BMC Credentials Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bmh-cred-cp01
  namespace: baremetal-operator-system
type: Opaque
stringData:
  username: ADMIN
  password: ADMIN
```

Apply:

```bash
kubectl apply -f bmh-secret.yaml
```

---

## Create BareMetalHost Resource

```yaml
apiVersion: metal3.io/v1alpha1
kind: BareMetalHost
metadata:
  name: cp01
  namespace: baremetal-operator-system
spec:
  online: true
  bootMACAddress: "ac:1f:6b:01:b7:21"
  bmc:
    address: "redfish://192.168.0.70/redfish/v1/Systems/1"
    credentialsName: bmh-cred-cp01
    disableCertificateVerification: true
  bootMode: UEFI
  rootDeviceHints:
    deviceName: /dev/sda
```

Apply:

```bash
kubectl apply -f bmh.yaml
```

---

# 🔎 Verify Inspection

```bash
kubectl get bmh -n baremetal-operator-system
kubectl describe bmh cp01 -n baremetal-operator-system
```

Wait until the host reaches:

```
State: available
```

Your management cluster is now ready for Daalu to provision workload clusters.
