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
import { errorText } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { SOT_NOT_SET_UP } from "@/components/operations/sot-not-set-up";

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
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [transportFilter, setTransportFilter] = useState<SotTransport | "all">(
    "all",
  );

  const devicesQ = useQuery({
    queryKey: ["sot-devices"],
    queryFn: () => api.sot.devices.list(),
  });
  const { data } = devicesQ;
  // Search, filters, the count and "Add device" only make sense once the
  // list loaded; while Nautobot isn't connected they'd be dead controls.
  const ready = !!data;

  const remove = useMutation({
    mutationFn: (d: SotDevice) => api.sot.devices.remove(d.id),
    onSuccess: (_r, d) => {
      qc.invalidateQueries({ queryKey: ["sot-devices"] });
      toast.success(`${d.name} removed from Nautobot`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const filtered = (data ?? []).filter((d) => {
    if (transportFilter !== "all" && d.transport !== transportFilter) return false;
    if (query && !d.name.toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="text-muted text-sm">
          Every device Daalu knows about, pulled live from your Source of
          Truth. Open a device to view and edit its intended config.
        </p>
        {ready && (
          <Button
            variant="primary"
            icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            onClick={() => setAdding(true)}
          >
            Add device
          </Button>
        )}
      </div>

      {ready && data.length > 0 && (
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs space-y-1">
            <span className="block text-muted">Search</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Hostname"
              className="text-sm h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line min-w-[220px]"
            />
          </label>
          <label className="text-xs space-y-1">
            <span className="block text-muted">Transport</span>
            <select
              value={transportFilter}
              onChange={(e) =>
                setTransportFilter(e.target.value as SotTransport | "all")
              }
              className="text-sm h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line"
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
          </label>
          <div className="text-xs text-muted pb-2.5" role="status">
            {filtered.length} of {data.length}
          </div>
        </div>
      )}

      {adding && <AddDeviceForm onClose={() => setAdding(false)} />}

      <LoadState
        query={devicesQ}
        loadingText="Loading devices…"
        notSetUp={SOT_NOT_SET_UP}
        empty={!!data && data.length === 0}
        emptyTitle="No devices yet"
        emptyHint={
          <>
            Click <strong>Add device</strong> to onboard your first one, or use
            the <strong>Bulk import</strong> tab to upload YAML or Excel.
          </>
        }
      >
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 motion-enter">
          {filtered.map((d) => (
            <DeviceTile
              key={d.id}
              device={d}
              removing={remove.isPending && remove.variables?.id === d.id}
              onRemove={() => remove.mutate(d)}
            />
          ))}
          {data && data.length > 0 && filtered.length === 0 && (
            <div className="surface p-6 text-center text-sm text-muted lg:col-span-2">
              No devices match the current filter.
            </div>
          )}
        </div>
      </LoadState>
    </div>
  );
}

function DeviceTile({
  device,
  onRemove,
  removing,
}: {
  device: SotDevice;
  onRemove: () => void;
  removing: boolean;
}) {
  const Icon = TRANSPORT_ICON[device.transport] ?? Cloud;
  return (
    <div className="group relative block w-full rounded-2xl overflow-hidden border border-line bg-bg-card hover:border-accent-blue/40 transition-colors shadow-sm">
      <Link href={`/devices/${device.id}`} className="block pl-5 pr-4 pt-3.5 pb-2">
        <div className="flex items-center gap-2 mb-1.5">
          <Icon className="h-3.5 w-3.5 text-accent-cyan" aria-hidden />
          <span className="text-[11px] uppercase tracking-wider text-muted">
            {TRANSPORT_LABEL[device.transport] ?? device.transport}
          </span>
          <span className="ml-auto text-xs text-muted">
            {device.primary_ip || "no IP"}
          </span>
        </div>
        <h3 className="font-medium text-[14px] leading-snug text-[color:var(--text)]">
          {device.name}
        </h3>
        <div className="flex items-center gap-2 mt-2 text-xs text-muted">
          <span className="font-mono">{device.id.slice(0, 8)}</span>
          {device.tags.length > 0 && (
            <span className="truncate">· tags: {device.tags.join(", ")}</span>
          )}
          <span className="ml-auto inline-flex items-center gap-1 group-hover:text-[color:var(--text)]">
            Open <ChevronRight className="h-3 w-3" aria-hidden />
          </span>
        </div>
      </Link>
      <div className="flex justify-end px-4 pb-3">
        <ConfirmButton
          label="Remove"
          confirmLabel={`Remove ${device.name}`}
          consequence="Deletes it from Nautobot; Daalu stops managing it. The device itself is untouched."
          icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
          tone="danger"
          pending={removing}
          onConfirm={onRemove}
        />
      </div>
    </div>
  );
}

function AddDeviceForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
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
      toast.success(`${name} added to Nautobot`);
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
    <div className="surface p-5 space-y-3 border-accent-blue/40 motion-enter">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold inline-flex items-center gap-2">
            <Plus className="h-4 w-4 text-accent-cyan" /> Add a device
          </h2>
          <p className="text-xs text-muted mt-0.5">
            Creates the device in Nautobot with its transport set and assigns
            the primary IP. Then open the device to write its intended config.
          </p>
        </div>
        <Button
          variant="icon"
          aria-label="Close add-device form"
          onClick={onClose}
          icon={<X className="h-4 w-4" aria-hidden />}
        />
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
          help="How Daalu connects to it. Saved on the Nautobot device."
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
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          onClick={() => create.mutate()}
          disabled={!canSubmit}
          disabledReason={canSubmit ? undefined : "Fill in name, primary IP, site, type and role first"}
          pending={create.isPending}
        >
          Create device
        </Button>
      </div>

      {create.isError && (
        <div role="alert" className="text-xs text-[color:var(--critical)]">
          {errorText(create.error)}
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
      {help && <div className="text-xs text-muted">{help}</div>}
    </label>
  );
}
