import { redirect } from "next/navigation";

/**
 * Devices used to be its own top-level page. The list view now lives
 * inside /operations as the Devices tab. Detail pages (/devices/[id])
 * still resolve directly.
 */
export default function DevicesIndexRedirect() {
  redirect("/operations?tab=devices");
}
