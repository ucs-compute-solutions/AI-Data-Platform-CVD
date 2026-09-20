apiVersion: vast.io/v1
kind: VmsView
metadata:
  name: vss-video-chunks
  namespace: __CVD_CONTROL_NAMESPACE__
  labels:
    app.kubernetes.io/part-of: vast-native-vss
spec:
  bucketName: video-chunks
  bucketOwner: __CVD_BUCKET_OWNER__
  createDir: true
  deletionPolicy: Retain
  path: /video-chunks
  policyName: __CVD_VIEW_POLICY__
  protocols: [S3]
  s3VisibilityGroups: [__CVD_VISIBILITY_GROUP__]
  tenantName: __CVD_TENANT_NAME__
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
---
apiVersion: vast.io/v1
kind: VmsView
metadata:
  name: vss-video-chunks-segments
  namespace: __CVD_CONTROL_NAMESPACE__
  labels:
    app.kubernetes.io/part-of: vast-native-vss
spec:
  bucketName: video-chunks-segments
  bucketOwner: __CVD_BUCKET_OWNER__
  createDir: true
  deletionPolicy: Retain
  path: /video-chunks-segments
  policyName: __CVD_VIEW_POLICY__
  protocols: [S3]
  s3VisibilityGroups: [__CVD_VISIBILITY_GROUP__]
  tenantName: __CVD_TENANT_NAME__
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
---
apiVersion: vast.io/v1
kind: VmsView
metadata:
  name: vss-processed-videos-db
  namespace: __CVD_CONTROL_NAMESPACE__
  labels:
    app.kubernetes.io/part-of: vast-native-vss
spec:
  bucketName: processed-videos-db
  bucketOwner: __CVD_BUCKET_OWNER__
  createDir: true
  deletionPolicy: Retain
  path: /processed-videos-db
  policyName: __CVD_VIEW_POLICY__
  protocols: [S3, DATABASE]
  s3VisibilityGroups: [__CVD_VISIBILITY_GROUP__]
  tenantName: __CVD_TENANT_NAME__
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
---
apiVersion: vast.io/v1
kind: VmsVdbView
metadata:
  name: vss-processed-videos-db
  namespace: __CVD_CONTROL_NAMESPACE__
  labels:
    app.kubernetes.io/part-of: vast-native-vss
spec:
  bucketName: processed-videos-db
  bucketOwner: __CVD_BUCKET_OWNER__
  cascadeDelete: false
  createDir: true
  deletionPolicy: Retain
  path: /processed-videos-db
  schemaName: processed-videos-schema
  tenantName: __CVD_TENANT_NAME__
  viewPolicy: __CVD_VIEW_POLICY__
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
