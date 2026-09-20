apiVersion: v1
kind: Namespace
metadata:
  name: __NAMESPACE__
  labels:
    app.kubernetes.io/part-of: nvidia-vss-search
    cvd.cisco.com/workload: nvidia-vss-3.2.1-search
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: nvidia-vss-anyuid
  namespace: __NAMESPACE__
  annotations:
    cvd.cisco.com/reason: NVIDIA-VSS-3.2.1-fixed-and-root-UID-workloads
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:openshift:scc:anyuid
subjects:
  - kind: ServiceAccount
    name: default
    namespace: __NAMESPACE__
  - kind: ServiceAccount
    name: sdrc
    namespace: __NAMESPACE__
