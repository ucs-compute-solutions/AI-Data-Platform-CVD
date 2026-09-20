apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-ui
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  to:
    kind: Service
    name: vss-agent-ui
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-ui-chat-api
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /api/chat
  to:
    kind: Service
    name: vss-agent-ui
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-agent-api
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /api
  to:
    kind: Service
    name: vss-agent
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-agent-chat
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /chat
  to:
    kind: Service
    name: vss-agent
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-agent-websocket
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /websocket
  to:
    kind: Service
    name: vss-agent
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-agent-static
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /static
  to:
    kind: Service
    name: vss-agent
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-vst
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __ROUTE_HOST__
  path: /vst
  to:
    kind: Service
    name: vss-vios-ingress
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-video-analytics-api
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
    haproxy.router.openshift.io/rewrite-target: /
spec:
  host: __ROUTE_HOST__
  path: /video-analytics-api
  to:
    kind: Service
    name: vss-video-analytics-api
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vss-streamer
  namespace: __NAMESPACE__
  annotations:
    haproxy.router.openshift.io/timeout: 3600s
spec:
  host: __STREAMER_ROUTE_HOST__
  to:
    kind: Service
    name: vss-vios-nvstreamer
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
