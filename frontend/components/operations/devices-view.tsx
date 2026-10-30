"use client";

/**
 * Devices list — every device in the tenant's SoT. Used inside the
 * /operations Devices tab. Backed by /sot/devices, which proxies
 * NautobotSoT.list_devices.
 */

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  Cloud,
  HardDrive,
  Network,
  Plus,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { api, type SotDevice, type SotTransport } from "@/lib/api";

const TRANSPORT_LABEL: Record<SotTransport, string> = {
  linux_ssh: "Linux (SSH)",
  redfish: "Server BMC (Redfish)",
  junos: "Juniper Junos",
  iosxr: "Cisco IOS-XR",
  eos: "Arista EOS",
  unknown: "Unknown",
};

const TRANSPORT_ICON: Record<SotTransport, typeof HardDrive> = {
  linux_ssh: Server,
  redfish: HardDrive,
  junos: Network,
  iosxr: Network,
  eos: Network,
  unknown: Cloud,
};

export function DevicesView() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [transportFilter, setTransportFilter] = useState<SotTransport | "all">(
    "all",
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["sot-devices"],
    queryFn: () => api.sot.devices.list(),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.sot.devices.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sot-devices"] }),
  });

  const filtered = (data ?? []).filter((d) => {
    if (transportFilter !== "all" && d.transport !== transportFilter) return false;
    if (query && !d.name.toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-muted text-sm">
            Every device daalu knows about — pulled live from your Source of
            Truth. Click any tile to view + edit its intended config.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5"
        >
          <Plus className="h-3.5 w-3.5" /> Add device
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by hostname"
          className="text-xs h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line min-w-[220px]"
        />
        <select
          value={transportFilter}
          onChange={(e) =>
            setTransportFilter(e.target.value as SotTransport | "all")
          }
          className="text-xs h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line"
        >
          <option value="all">All transports</option>
          {Object.entries(TRANSPORT_LABEL)
            .filter(([k]) => k !== "unknown")
            .map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
        </select>
        <div className="text-xs text-muted self-center">
          {filtered.length} of {data?.length ?? 0}
        </div>
      </div>

      {adding && <AddDeviceForm onClose={() => setAdding(false)} />}

      {isLoading && <div className="text-sm text-muted">Loading devices…</div>}
      {error && (
        <div className="surface p-4 text-sm text-[color:var(--critical)]">
          Couldn't load devices: {String(error)}
          <div className="mt-2 text-[12px] text-muted">
            If you haven't set up Nautobot yet, head to{" "}
            <Link href="/onboarding/sot" className="underline text-accent-cyan">
              the SoT wizard
            </Link>{" "}
            first.
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {filtered.map((d) => (
          <DeviceTile
            key={d.id}
            device={d}
            onRemove={() => {
              if (
                confirm(
                  `Delete ${d.name} from Nautobot? Daalu will stop managing it; existing data on the device itself is untouched.`,
                )
              ) {
                remove.mutate(d.id);
              }
            }}
          />
        ))}
        {data && data.length === 0 && !isLoading && (
          <div className="surface p-6 text-center text-sm text-muted">
            <p>
              No devices yet. Click <strong>Add device</strong> to onboard your
              first one, or use the <strong>Bulk import</strong> tab to upload
              YAML/Excel.
            </p>
          </div>
        )}
        {data && data.length > 0 && filtered.length === 0 && (
          <div className="surface p-6 text-center text-sm text-muted">
            No devices match the current filter.
          </div>
        )}
      </div>
    </div>
  );
}

function DeviceTile({
  device,
  onRemove,
}: {
  device: SotDevice;
  onRemove: () => void;
}) {
  const Icon = TRANSPORT_ICON[device.transport] ?? Cloud;
  return (
    <div
      className="group relative block w-full rounded-2xl overflow-hidden border border-line bg-bg-card hover:border-accent-blue/40 transition-colors"
      style={{
        boxShadow:
          "0 1px 0 rgba(0,0,0,0.25), 0 8px 24px -12px rgba(0,0,0,0.55)",
      }}
    >
      <Link href={`/devices/${device.id}`} className="block pl-5 pr-4 py-3.5">
        <div className="flex items-center gap-2 mb-1.5">
          <Icon className="h-3.5 w-3.5 text-accent-cyan" />
          <span className="text-[10px] uppercase tracking-wider text-muted">
            {TRANSPORT_LABEL[device.transport] ?? device.transport}
          </span>
          <span className="ml-auto text-[10px] text-muted">
            {device.primary_ip || "no IP"}
          </span>
        </div>
        <h3 className="font-medium text-[14px] leading-snug text-[color:var(--text)]">
          {device.name}
        </h3>
        <div className="flex items-center gap-2 mt-2 text-[11px] text-muted">
          <span className="font-mono">{device.id.slice(0, 8)}</span>
          {device.tags.length > 0 && (
            <span className="truncate">· tags: {device.tags.join(", ")}</span>
          )}
          <span className="ml-auto inline-flex items-center gap-1 group-hover:text-[color:var(--text)]">
            Open <ChevronRight className="h-3 w-3" />
          </span>
        </div>
      </Link>
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onRemove();
        }}
        className="absolute top-2 right-2 h-7 w-7 rounded-md text-muted hover:text-[color:var(--critical)] hover:bg-bg-elevated/60 inline-flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
        title="Delete device from Nautobot"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function AddDeviceForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [primaryIp, setPrimaryIp] = useState("");
  const [transport, setTransport] = useState<SotTransport>("linux_ssh");
  const [siteId, setSiteId] = useState("");
  const [deviceTypeId, setDeviceTypeId] = useState("");
  const [deviceRoleId, setDeviceRoleId] = useState("");
  const [platformId, setPlatformId] = useState("");

  const { data: catalog, isLoading: catalogLoading } = useQuery({
    queryKey: ["sot-catalog"],
    queryFn: () => api.sot.devices.catalog(),
  });

  const create = useMutation({
    mutationFn: () =>
      api.sot.devices.create({
        name,
        primary_ip: primaryIp,
        site_id: siteId,
        device_type_id: deviceTypeId,
        device_role_id: deviceRoleId,
        platform_id: platformId || null,
        transport,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sot-devices"] });
      onClose();
    },
  });

  const canSubmit =
    name.length > 0 &&
    primaryIp.length > 0 &&
    siteId.length > 0 &&
    deviceTypeId.length > 0 &&
    deviceRoleId.length > 0;

  return (
    <div className="surface p-5 space-y-3 border-accent-blue/40">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold inline-flex items-center gap-2">
            <Plus className="h-4 w-4 text-accent-cyan" /> Add a device
          </h2>
          <p className="text-[12px] text-muted mt-0.5">
            Creates the device row in Nautobot with daalu_transport pre-set,
            then assigns the primary IP. After it lands, click into the device
            to author its intended config.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-muted hover:text-[color:var(--text)]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {catalogLoading && (
        <div className="text-xs text-muted">Loading catalog…</div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Device name" value={name} onChange={setName} placeholder="web01" />
        <Field
          label="Primary IP (CIDR)"
          value={primaryIp}
          onChange={setPrimaryIp}
          placeholder="10.0.0.5/24"
        />
        <Select
          label="Transport"
          value={transport}
          onChange={(v) => setTransport(v as SotTransport)}
          options={[
            { value: "linux_ssh", label: "Linux (SSH)" },
            { value: "redfish", label: "Server BMC (Redfish)" },
            { value: "junos", label: "Juniper Junos" },
            { value: "iosxr", label: "Cisco IOS-XR" },
            { value: "eos", label: "Arista EOS" },
          ]}
          help="Stamped as daalu_transport custom field on the Nautobot device."
        />
        <Select
          label="Site / location"
          value={siteId}
          onChange={setSiteId}
          options={(catalog?.sites ?? []).map((s) => ({
            value: s.id,
            label: s.name,
          }))}
          placeholder={
            catalog?.sites.length === 0 ? "No sites in Nautobot" : "Pick a site"
          }
        />
        <Select
          label="Device type"
          value={deviceTypeId}
          onChange={setDeviceTypeId}
          options={(catalog?.device_types ?? []).map((d) => ({
            value: d.id,
            label: d.name,
          }))}
          placeholder={
            catalog?.device_types.length === 0
              ? "No device types in Nautobot"
              : "Pick a type"
          }
        />
        <Select
          label="Device role"
          value={deviceRoleId}
          onChange={setDeviceRoleId}
          options={(catalog?.device_roles ?? []).map((r) => ({
            value: r.id,
            label: r.name,
          }))}
          placeholder={
            catalog?.device_roles.length === 0
              ? "No roles in Nautobot"
              : "Pick a role"
          }
        />
        <Select
          label="Platform (optional)"
          value={platformId}
          onChange={setPlatformId}
          options={[
            { value: "", label: "— none —" },
            ...(catalog?.platforms ?? []).map((p) => ({
              value: p.id,
              label: p.name,
            })),
          ]}
        />
      </div>

      <div className="flex justify-end gap-2 pt-2 border-t border-line/40">
        <button
          type="button"
          onClick={onClose}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => create.mutate()}
          disabled={!canSubmit || create.isPending}
          className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          {create.isPending ? "Creating…" : "Create device"}
        </button>
      </div>

      {create.isError && (
        <div className="text-[11.5px] text-[color:var(--critical)]">
          {String(create.error)}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="text-xs space-y-1">
      <div className="text-muted uppercase tracking-wider">{label}</div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
      />
    </label>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  placeholder,
  help,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
  help?: string;
}) {
  return (
    <label className="text-xs space-y-1">
      <div className="text-muted uppercase tracking-wider">{label}</div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
      >
        {placeholder && <option value="">{placeholder}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {help && <div className="text-[10px] text-muted">{help}</div>}
    </label>
  );
}
