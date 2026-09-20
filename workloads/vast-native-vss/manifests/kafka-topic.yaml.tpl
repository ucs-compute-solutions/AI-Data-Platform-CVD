apiVersion: vast.io/v1
kind: VmsKafkaTopic
metadata:
  name: vss-video-events
  namespace: __CVD_CONTROL_NAMESPACE__
  labels:
    app.kubernetes.io/part-of: vast-native-vss
spec:
  brokerRef: __CVD_BROKER_RESOURCE__
  deletionPolicy: Retain
  partitions: 100
  retentionMs: 604800000
  schemaName: kafka_topics
  tenantName: __CVD_TENANT_NAME__
  topicName: __CVD_TOPIC_NAME__
  vmsCredentialsRef:
    mappings:
      host:
        configMapRef:
          key: __CVD_VMS_CONFIG_KEY__
          name: __CVD_VMS_CONFIGMAP__
          namespace: __CVD_CONTROL_NAMESPACE__
          path: __CVD_VMS_HOST_PATH__
      password:
        secretRef:
          key: __CVD_VMS_PASSWORD_KEY__
          name: __CVD_VMS_SECRET__
          namespace: __CVD_CONTROL_NAMESPACE__
      username:
        configMapRef:
          key: __CVD_VMS_CONFIG_KEY__
          name: __CVD_VMS_CONFIGMAP__
          namespace: __CVD_CONTROL_NAMESPACE__
          path: __CVD_VMS_USERNAME_PATH__
