{{/*
Expand the name of the chart.
*/}}
{{- define "cypherx-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Prefer the logical `service` name so resources read e.g. "auth-service".
Truncated at 63 chars (k8s DNS name limit) and trimmed of trailing dashes.
*/}}
{{- define "cypherx-service.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Values.service .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version label value.
*/}}
{{- define "cypherx-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Image tag / version. Falls back to Chart.AppVersion.
Used for both the container image tag and the VERSION env var (Contract 6).
*/}}
{{- define "cypherx-service.version" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{/*
Selector labels — the immutable subset used in matchLabels. NEVER add
version/environment here (a rolling deploy would otherwise orphan pods).
*/}}
{{- define "cypherx-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cypherx-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Standard labels applied to every object.
*/}}
{{- define "cypherx-service.labels" -}}
helm.sh/chart: {{ include "cypherx-service.chart" . }}
{{ include "cypherx-service.selectorLabels" . }}
app.kubernetes.io/version: {{ include "cypherx-service.version" . | quote }}
app.kubernetes.io/component: service
app.kubernetes.io/part-of: cypherx
app.kubernetes.io/managed-by: {{ .Release.Service }}
cypherx.ai/service: {{ .Values.service | quote }}
cypherx.ai/environment: {{ .Values.environment | quote }}
{{- end }}

{{/*
Name of the runtime ServiceAccount.
*/}}
{{- define "cypherx-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cypherx-service.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the dedicated migration-Job ServiceAccount (Component 16: the Job runs
with a SA that has access to the *_ddl secret only).
*/}}
{{- define "cypherx-service.migrationServiceAccountName" -}}
{{- printf "%s-migration" (include "cypherx-service.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Runtime Doppler-synced Secret name (holds runtime_password etc).
*/}}
{{- define "cypherx-service.runtimeSecretName" -}}
{{- if .Values.doppler.externalRuntimeSecretName }}
{{- .Values.doppler.externalRuntimeSecretName }}
{{- else if .Values.doppler.runtimeSecretName }}
{{- .Values.doppler.runtimeSecretName }}
{{- else }}
{{- printf "%s-runtime" (include "cypherx-service.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Migration-only Doppler-synced Secret name (holds the *_ddl password).
*/}}
{{- define "cypherx-service.ddlSecretName" -}}
{{- if .Values.doppler.externalDdlSecretName }}
{{- .Values.doppler.externalDdlSecretName }}
{{- else if .Values.doppler.ddlSecretName }}
{{- .Values.doppler.ddlSecretName }}
{{- else }}
{{- printf "%s-ddl" (include "cypherx-service.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Pod selector key for the topologySpreadConstraints / affinity blocks.
*/}}
{{- define "cypherx-service.podSelectorLabels" -}}
{{ include "cypherx-service.selectorLabels" . }}
{{- end }}

{{/*
Migration-Job label set. Identical to the standard labels EXCEPT
app.kubernetes.io/component is "migration". Use this on the migration Job, its
ServiceAccount, and the *_ddl DopplerSecret instead of appending a second
component key after `cypherx-service.labels` (which would emit a duplicate
YAML map key that strict validators — kubeconform, GitOps linters — reject).
*/}}
{{- define "cypherx-service.migrationLabels" -}}
helm.sh/chart: {{ include "cypherx-service.chart" . }}
{{ include "cypherx-service.selectorLabels" . }}
app.kubernetes.io/version: {{ include "cypherx-service.version" . | quote }}
app.kubernetes.io/component: migration
app.kubernetes.io/part-of: cypherx
app.kubernetes.io/managed-by: {{ .Release.Service }}
cypherx.ai/service: {{ .Values.service | quote }}
cypherx.ai/environment: {{ .Values.environment | quote }}
{{- end }}

{{/*
Doppler operator bootstrap-token Secret name. The G3 doppler-bootstrap stack +
the Doppler operator provision ONE token Secret per (env, namespace) named
`doppler-token-<namespace>` (Component 11). Default to that convention so a
service "just works" in its namespace; allow an explicit override for special
cases. Never hardcode a single shared name — that Secret would not exist.
*/}}
{{- define "cypherx-service.dopplerTokenSecretName" -}}
{{- .Values.doppler.tokenSecretName | default (printf "doppler-token-%s" .Release.Namespace) }}
{{- end }}
