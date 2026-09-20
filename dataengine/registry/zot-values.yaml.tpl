replicaCount: 1

ingress:
  className: ${INGRESS_CLASS}
  enabled: true
  annotations:
    route.openshift.io/termination: "edge"
    route.openshift.io/insecure-edge-termination-policy: "Redirect"
  hosts:
    - host: ${ZOT_HOST}
      paths:
        - path: /
          pathType: ImplementationSpecific

service:
  # Validated topology: the edge-terminated OpenShift Route fronts this
  # NodePort service.
  type: NodePort
  port: 5000

mountConfig: true
mountSecret: true

# deploy-zot.sh supplies secretFiles.htpasswd with --set-file. No credential or
# password hash is stored in this template.
configFiles:
  config.json: |-
    {
      "storage": { "rootDirectory": "/var/lib/registry" },
      "http": {
        "compat": ["docker2s2"],
        "address": "0.0.0.0",
        "port": "5000",
        "auth": { "htpasswd": { "path": "/secret/htpasswd" } },
        "accessControl": {
          "repositories": { "**": { "defaultPolicy": [] } },
          "adminPolicy": {
            "users": ["${ZOT_USER}"],
            "actions": ["read", "create", "update", "delete"]
          }
        }
      },
      "extensions": {
        "ui": { "enable": true },
        "search": { "enable": true }
      },
      "log": { "level": "info" }
    }

persistence: true
pvc:
  create: true
  name: zot
  storage: ${ZOT_STORAGE_SIZE}
  storageClassName: ${ZOT_STORAGE_CLASS}
  accessModes: ["ReadWriteOnce"]
