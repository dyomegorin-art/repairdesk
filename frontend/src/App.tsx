import { useCallback, useEffect, useState } from "react";
import { api, ApiError, AuditEntry, IDENTITIES, Me, RepairCase } from "./api";

type View = "new" | "engagements";

const STATUS_LABEL: Record<string, string> = {
  NEW: "Новая",
  NEEDS_INFORMATION: "Нужны данные",
  READY_FOR_REVIEW: "Готова к проверке",
  READY_TO_SCHEDULE: "Готова к записи",
};

const ELIGIBILITY_LABEL: Record<string, string> = {
  ELIGIBLE: "Допущена",
  CONDITIONAL: "С условиями",
  OWNER_REVIEW: "Решает владелец",
  LICENSE_HOLD: "Ждёт допуска мастера",
  DECLINE: "Отказ",
  EMERGENCY_STOP: "Аварийная остановка",
};

function auditTitle(a: AuditEntry): string {
  if (a.event_type === "WORKER_COMMAND_COMPLETED" && a.detail.command === "EVALUATE_ELIGIBILITY") {
    return "Worker: допуск оценён";
  }
  return EVENT_LABEL[a.event_type] ?? a.event_type;
}

function reasonCodes(a: AuditEntry): string {
  const r = a.detail.reasons;
  return Array.isArray(r) && r.length > 0 ? r.join(", ") : "";
}

const EVENT_LABEL: Record<string, string> = {
  DELIVERY_RECEIVED: "Доставка получена",
  INTAKE_EVENT_CREATED: "Событие приёма создано",
  CASE_CREATED: "Кейс создан",
  WORK_ITEM_CREATED: "Создана задача на проверку",
  DUPLICATE_IGNORED: "Повтор проигнорирован",
  INTAKE_STATUS_CHANGED: "Статус изменён",
  WORKER_COMMAND_COMPLETED: "Worker: проверка записи",
  WORKER_COMMAND_FAILED: "Worker: сбой",
  ACCESS_DENIED: "Доступ отклонён",
};

function describe(c: RepairCase): string {
  return c.units.map((u) => [u.appliance_type, u.brand].filter(Boolean).join(" · ")).join("; ") || "—";
}

function time(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
}

export function App() {
  const [identity, setIdentity] = useState(() => localStorage.getItem("identity") ?? IDENTITIES[0]);
  const [me, setMe] = useState<Me | null>(null);
  const [view, setView] = useState<View>("new");
  const [cases, setCases] = useState<RepairCase[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setMe(await api.me(identity));
      setCases(await (view === "new" ? api.newRequests(identity) : api.engagements(identity)));
    } catch (e) {
      setCases([]);
      setError(
        e instanceof ApiError && e.status === 403
          ? view === "new"
            ? "Доступ запрещён: у этой роли нет доступа к новым заявкам."
            : "Доступ запрещён: у этой роли нет доступа к активным работам."
          : "Не удалось загрузить данные. Проверьте, что backend запущен.",
      );
    }
  }, [identity, view]);

  useEffect(() => {
    void load();
  }, [load]);

  const changeIdentity = (id: string) => {
    localStorage.setItem("identity", id);
    setIdentity(id);
    setSelected(null);
  };

  return (
    <div className="app">
      <header className="top">
        <h1>RepairDesk</h1>
        <label className="who">
          Тестовый пользователь
          <select value={identity} onChange={(e) => changeIdentity(e.target.value)}>
            {IDENTITIES.map((i) => (
              <option key={i}>{i}</option>
            ))}
          </select>
        </label>
        {me && <span className="role">роль: {me.role}</span>}
      </header>
      <p className="banner">Учебный проект: все данные вымышлены. Права проверяет backend, а не интерфейс.</p>

      <nav className="tabs">
        <button className={view === "new" ? "on" : ""} onClick={() => { setView("new"); setSelected(null); }}>
          Новые заявки
        </button>
        <button className={view === "engagements" ? "on" : ""} onClick={() => { setView("engagements"); setSelected(null); }}>
          Активные работы
        </button>
        <button className="ghost" onClick={() => void load()}>Обновить</button>
      </nav>

      {error && <p className="error" role="alert">{error}</p>}

      <main className={selected ? "split" : ""}>
        <section>
          {!error && cases.length === 0 && (
            <p className="empty">
              {view === "engagements" && me && !me.permissions.includes("queue.view")
                ? "Вам пока не назначено ни одной работы."
                : "Пока ничего нет."}
            </p>
          )}
          <ul className="list">
            {cases.map((c) => (
              <li key={c.case_id}>
                <button className={c.case_id === selected ? "row on" : "row"} onClick={() => setSelected(c.case_id)}>
                  <strong>{c.contact_display_name ?? "Без имени"}</strong>
                  <span>{describe(c)}</span>
                  <span className="meta">
                    {c.location_type ?? "тип объекта?"} · {c.source_attribution} · {time(c.created_at)}
                  </span>
                  <span className={`chip ${c.intake_status}`}>{STATUS_LABEL[c.intake_status]}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        {selected && me && (
          <CaseDetail key={selected} identity={identity} me={me} caseId={selected} onChanged={load} />
        )}
      </main>
    </div>
  );
}

function CaseDetail(props: { identity: string; me: Me; caseId: string; onChanged: () => Promise<void> }) {
  const { identity, me, caseId, onChanged } = props;
  const [c, setC] = useState<RepairCase | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setC(await api.caseDetail(identity, caseId));
      // Only roles with the audit permission request the timeline (technicians have none).
      setAudit(me.permissions.includes("audit.view") ? await api.audit(identity, caseId) : []);
      setMsg(null);
    } catch (e) {
      setMsg(e instanceof ApiError && e.status === 403 ? "Нет права открыть кейс." : "Ошибка загрузки.");
    }
  }, [identity, caseId, me]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setStatus = async (s: "NEEDS_INFORMATION" | "READY_FOR_REVIEW") => {
    try {
      await api.setStatus(identity, caseId, s);
      await refresh();
      await onChanged();
    } catch (e) {
      setMsg(e instanceof ApiError && e.status === 403 ? "Нет права менять статус." : "Не удалось изменить статус.");
      await refresh();
    }
  };

  if (!c) return <aside className="detail">{msg ?? "Загрузка…"}</aside>;
  const canEdit = me.permissions.includes("case.update_intake_status") && c.case_phase === "INTAKE";

  return (
    <aside className="detail">
      <h2>{c.contact_display_name ?? "Без имени"}</h2>
      <dl>
        <dt>Фаза</dt><dd>{c.case_phase}</dd>
        <dt>Статус</dt><dd>{STATUS_LABEL[c.intake_status]}</dd>
        <dt>Допуск</dt><dd>{c.eligibility_result ? `${ELIGIBILITY_LABEL[c.eligibility_result] ?? c.eligibility_result} (${c.eligibility_result})` : "ещё не оценена"}</dd>
        <dt>Объект</dt><dd>{c.location_type ?? "—"}</dd>
        <dt>Источник</dt><dd>{c.source_attribution ?? "—"}</dd>
        <dt>Мастер</dt><dd>{c.assigned_technician_id ?? "не назначен"}</dd>
        <dt>ID кейса</dt><dd className="mono">{c.case_id}</dd>
      </dl>
      <h3>Техника</h3>
      <ul>
        {c.units.map((u, i) => (
          <li key={i}>{[u.appliance_type, u.brand, u.problem].filter(Boolean).join(" · ")}</li>
        ))}
      </ul>
      <div className="actions">
        <button disabled={!canEdit} onClick={() => void setStatus("NEEDS_INFORMATION")}>Нужны данные</button>
        <button disabled={!canEdit} onClick={() => void setStatus("READY_FOR_REVIEW")}>Готова к проверке</button>
      </div>
      {!canEdit && <p className="hint">Кнопки недоступны для этой роли или фазы.</p>}
      {msg && <p className="error" role="alert">{msg}</p>}
      {me.permissions.includes("audit.view") && <h3>Журнал аудита</h3>}
      <ol className="timeline">
        {audit.map((a) => (
          <li key={a.id}>
            <strong>{auditTitle(a)}</strong>
            <span className="meta">{a.actor} · {time(a.created_at)}</span>
            {reasonCodes(a) && <span className="meta">{reasonCodes(a)}</span>}
          </li>
        ))}
      </ol>
    </aside>
  );
}
