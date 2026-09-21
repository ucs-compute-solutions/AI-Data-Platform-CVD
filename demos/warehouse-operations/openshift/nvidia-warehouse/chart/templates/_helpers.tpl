{{/* Chart and resource names. */}}
{{- define "nvidia-warehouse.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nvidia-warehouse.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "nvidia-warehouse.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "nvidia-warehouse.componentName" -}}
{{- printf "%s-%s" (include "nvidia-warehouse.fullname" .root) (.name | kebabcase) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nvidia-warehouse.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | quote }}
app.kubernetes.io/name: {{ include "nvidia-warehouse.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: {{ .component | kebabcase }}
warehouse.nvidia.com/status: render-only-incomplete
{{- end -}}

{{- define "nvidia-warehouse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nvidia-warehouse.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component | kebabcase }}
{{- end -}}

{{/* Stop an accidental opt-in that omits the explicit incomplete-design acknowledgement. */}}
{{- define "nvidia-warehouse.assertSafety" -}}
{{- if and .Values.global.enabled (not .Values.global.acknowledgeIncomplete) -}}
{{- fail "global.enabled=true requires global.acknowledgeIncomplete=true; this chart is an incomplete render skeleton" -}}
{{- end -}}
{{- end -}}

{{/* Reject latest even when schema validation is bypassed. */}}
{{- define "nvidia-warehouse.image" -}}
{{- $repository := required "image repository is required" .image.repository -}}
{{- if .image.digest -}}
{{- printf "%s@%s" $repository .image.digest -}}
{{- else -}}
{{- if not .root.Values.global.allowTagOnlyRender -}}
{{- fail "enabled components require immutable image digests; set global.allowTagOnlyRender=true only for local render inspection" -}}
{{- end -}}
{{- $tag := required "an explicit image tag or digest is required" (toString .image.tag) -}}
{{- if eq $tag "latest" -}}
{{- fail "the image tag latest is prohibited" -}}
{{- end -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "nvidia-warehouse.serviceAccountName" -}}
{{- $account := index .root.Values.serviceAccounts .key -}}
{{- if not $account -}}
{{- fail (printf "unknown service account key %s" .key) -}}
{{- end -}}
{{- required (printf "serviceAccounts.%s.name is required" .key) $account.name -}}
{{- end -}}

{{- define "nvidia-warehouse.probeAction" -}}
{{- if eq .probe.type "http" }}
httpGet:
  path: {{ .probe.path | default "/" | quote }}
  port: {{ .probe.port }}
{{- else if eq .probe.type "tcp" }}
tcpSocket:
  port: {{ .probe.port }}
{{- else }}
{{- fail (printf "unsupported probe type %s" .probe.type) }}
{{- end }}
{{- end -}}
