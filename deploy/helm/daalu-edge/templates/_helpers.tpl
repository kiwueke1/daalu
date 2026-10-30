{{- define "daalu-edge.name" -}}
daalu-edge
{{- end -}}

{{- define "daalu-edge.labels" -}}
app.kubernetes.io/name: daalu-edge
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "daalu-edge.required" -}}
{{- if not .Values.cluster.slug -}}{{ fail "cluster.slug is required" }}{{- end -}}
{{- if not .Values.cluster.tunnelIp -}}{{ fail "cluster.tunnelIp is required" }}{{- end -}}
{{- if not .Values.hub.endpoint -}}{{ fail "hub.endpoint is required" }}{{- end -}}
{{- if not .Values.hub.publicKey -}}{{ fail "hub.publicKey is required" }}{{- end -}}
{{- if not .Values.bootstrap.token -}}{{ fail "bootstrap.token is required on first install" }}{{- end -}}
{{- /* Reject port collisions between apiserver-proxy and any
       serviceProxies[] entry — they all bind in the same pod netns. */}}
{{- $ports := dict -}}
{{- if .Values.apiserverProxy.enabled -}}
{{- $_ := set $ports (toString .Values.apiserverProxy.listenPort) "apiserverProxy" -}}
{{- end -}}
{{- range $i, $p := .Values.serviceProxies -}}
{{- $port := toString $p.listenPort -}}
{{- if hasKey $ports $port -}}{{ fail (printf "serviceProxies[%d].listenPort=%s collides with %s" $i $port (index $ports $port)) }}{{- end -}}
{{- $_ := set $ports $port (printf "serviceProxies[%d]" $i) -}}
{{- end -}}
{{- end -}}
