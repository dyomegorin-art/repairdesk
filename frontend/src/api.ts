export type IntakeStatus = "NEW" | "NEEDS_INFORMATION" | "READY_FOR_REVIEW" | "READY_TO_SCHEDULE";

export interface Unit {
  appliance_type?: string;
  brand?: string;
  problem?: string;
}

export interface RepairCase {
  case_id: string;
  case_phase: string;
  intake_status: IntakeStatus;
  eligibility_result: string | null;
  source_attribution: string | null;
  assigned_technician_id: string | null;
  contact_display_name: string | null;
  location_type: string | null;
  units: Unit[];
  created_at: string | null;
}

export interface AuditEntry {
  id: number;
  event_type: string;
  actor: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface Me {
  identity_id: string;
  role: string;
  permissions: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Synthetic identities are pre-seeded on the backend (local/test only).
// The UI sends only an identity id; the role is resolved server-side.
export const IDENTITIES = ["disp-1", "owner-1", "tech-1", "tech-2", "dev-1"];

async function request<T>(identity: string, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Synthetic-Identity": identity, ...init.headers },
  });
  if (!res.ok) throw new ApiError(res.status, res.status === 403 ? "forbidden" : `HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  me: (id: string) => request<Me>(id, "/api/me"),
  newRequests: (id: string) => request<RepairCase[]>(id, "/api/queues/new-requests"),
  engagements: (id: string) => request<RepairCase[]>(id, "/api/queues/engagements"),
  caseDetail: (id: string, caseId: string) => request<RepairCase>(id, `/api/cases/${caseId}`),
  audit: (id: string, caseId: string) => request<AuditEntry[]>(id, `/api/cases/${caseId}/audit`),
  setStatus: (id: string, caseId: string, intake_status: "NEEDS_INFORMATION" | "READY_FOR_REVIEW") =>
    request<RepairCase>(id, `/api/cases/${caseId}/intake-status`, {
      method: "PATCH",
      body: JSON.stringify({ intake_status }),
    }),
};
