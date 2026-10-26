import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  Delivery,
  DeliveryStatus,
  ProtocolSignature,
  SessionDetail,
  SessionProtocol,
  SignatureSummary,
  VerifyResult,
  VerifyStatus,
} from "../api";

/**
 * FR-40/41 (docs/18 §7, §8): документ приёма — подпись, проверка, PDF, передача в ЕМИАС.
 *
 * Состояние берётся из `SessionDetail` (свежая деталь после каждой мутации): подпись и
 * доставка относятся к активной версии протокола, поэтому при смене версии старые
 * результаты проверки и статусы сбрасываются, а не показываются как «текущие».
 */

const ROLE_LABELS: Record<string, string> = {
  clinician: "Врач",
  mo: "Медицинская организация",
};

const VERIFY_LABELS: Record<VerifyStatus, string> = {
  valid: "подпись действительна",
  invalid: "подпись недействительна",
  unknown_ca: "сертификат выдан неизвестным УЦ",
  expired: "срок действия сертификата истёк",
  digest_mismatch: "хэш документа не совпадает",
};

const VERIFY_TONE: Record<VerifyStatus, string> = {
  valid: "GREEN",
  invalid: "RED",
  unknown_ca: "YELLOW",
  expired: "RED",
  digest_mismatch: "RED",
};

const DELIVERY_LABELS: Record<DeliveryStatus, string> = {
  queued: "в очереди",
  sending: "отправляется",
  registered: "зарегистрирован в РЭМД",
  rejected: "отклонён РЭМД",
  failed: "ошибка отправки",
};

const DELIVERY_TONE: Record<DeliveryStatus, string> = {
  queued: "YELLOW",
  sending: "YELLOW",
  registered: "GREEN",
  rejected: "RED",
  failed: "RED",
};

const DELIVERY_ERRORS: Record<string, string> = {
  EMIAS_DISABLED: "Передача в ЕМИАС выключена администратором",
  EMIAS_UNAVAILABLE: "РЭМД недоступен — повторите отправку",
  PROTOCOL_NOT_SIGNED: "Сначала подпишите протокол",
  PROTOCOL_NOT_APPROVED: "Подписать можно только подтверждённый протокол",
  PROTOCOL_ALREADY_SIGNED: "Протокол уже подписан",
  SIGNER_UNAVAILABLE: "Демо-УЦ недоступен — обратитесь к администратору",
  CALLBACK_TIMEOUT: "РЭМД не подтвердил регистрацию за отведённое время — повторите отправку",
};

/** Ошибка документа по-русски: код уходит в журнал, наружу — объяснение. */
function documentError(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return DELIVERY_ERRORS[e.code] ?? (e.detail || fallback);
  return e instanceof Error && e.message ? e.message : fallback;
}

const STAMP = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });

const when = (iso: string | null | undefined) => (iso ? STAMP.format(new Date(iso)) : "—");

const DELIVERY_POLL_MS = 2000;
const DELIVERY_POLL_LIMIT_MS = 10 * 60 * 1000;



function SignatureBlock({ signature }: { signature: ProtocolSignature }) {
  return (
    <div className="signature-block">
      <p className="subhead">
        <span className="badge GREEN">ПОДПИСАНО</span> {ROLE_LABELS[signature.role] ?? signature.role}
      </p>
      <dl className="protocol-view">
        <dt>ФИО</dt>
        <dd>{signature.fio}</dd>
        <dt>Должность</dt>
        <dd>{signature.position}</dd>
        <dt>СНИЛС</dt>
        <dd>{signature.snils || "не указан"}</dd>
        <dt>Издатель сертификата</dt>
        <dd>{signature.cert_issuer}</dd>
        <dt>Серийный номер</dt>
        <dd>{signature.cert_serial}</dd>
        <dt>Срок действия</dt>
        <dd>
          {when(signature.cert_not_before)} — {when(signature.cert_not_after)}
        </dd>
        <dt>Отпечаток</dt>
        <dd>
          <code>{signature.cert_thumbprint.slice(0, 16)}…</code>
        </dd>
        <dt>Время подписи</dt>
        <dd>{when(signature.signed_at)}</dd>
      </dl>
    </div>
  );
}

export default function SessionDocumentCard({
  sessionId,
  protocol,
  signature,
  delivery,
  onRefresh,
}: {
  sessionId: string;
  protocol: SessionProtocol | null;
  signature: SignatureSummary | null;
  delivery: Delivery | null;
  /** Свежая деталь сессии — источник подписи и доставки. */
  onRefresh: () => Promise<SessionDetail | null>;
}) {
  const [busy, setBusy] = useState<"" | "sign" | "verify" | "pdf" | "submit" | "status">("");
  // Результат проверки привязан к версии протокола: после новой версии он неактуален.
  const [verified, setVerified] = useState<{ version: number; result: VerifyResult } | null>(null);
  // Локальный статус доставки перекрывает проп до следующего refresh; смена версии — сброс.
  const [liveDelivery, setLiveDelivery] = useState<Delivery | null>(null);
  const [polling, setPolling] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const alive = useRef(true);
  const pollTimer = useRef<number | null>(null);
  const pollDeadline = useRef(0);
  const version = protocol?.version ?? 0;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    };
  }, []);

  useEffect(() => {
    setVerified(null);
    setLiveDelivery(null);
    setNote("");
    setErr("");
  }, [version]);

  const current = liveDelivery ?? delivery;
  const signed = signature !== null;
  const verifyShown = verified && protocol && verified.version === protocol.version ? verified.result : null;
  const terminal = current !== null && ["registered", "rejected"].includes(current.status);

  const toast = (text: string) => {
    setNote(text);
    setErr("");
  };

  const sign = async () => {
    setBusy("sign");
    setErr("");
    setNote("");
    try {
      const result = await api.signProtocol(sessionId);
      await onRefresh();
      setVerified(null);
      const roles = result.signature.signatures
        .map((item) => ROLE_LABELS[item.role] ?? item.role)
        .join(" + ");
      toast(`Протокол подписан: ${roles}.`);
    } catch (e: unknown) {
      setErr(documentError(e, "Не удалось подписать протокол"));
    } finally {
      setBusy("");
    }
  };

  const verify = async () => {
    setBusy("verify");
    setErr("");
    try {
      const result = await api.verifyProtocol(sessionId);
      setVerified({ version, result });
    } catch (e: unknown) {
      setErr(documentError(e, "Не удалось проверить подпись"));
    } finally {
      setBusy("");
    }
  };

  const downloadPdf = async () => {
    setBusy("pdf");
    setErr("");
    setNote("");
    try {
      const { blob, filename } = await api.protocolPdf(sessionId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || `protocol-${version || "?"}.pdf`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      const size = blob.size >= 1024 ? `${Math.round(blob.size / 1024)} КБ` : `${blob.size} Б`;
      toast(`PDF скачан (${size}).`);
    } catch (e: unknown) {
      setErr(documentError(e, "Не удалось скачать PDF"));
    } finally {
      setBusy("");
    }
  };

  /** §8.3: опрос доставки до терминального статуса; цепочка снимается при unmount. */
  const deliveryTick = async () => {
    pollTimer.current = null;
    if (!alive.current) return;
    if (Date.now() > pollDeadline.current) {
      setPolling(false);
      setNote("РЭМД пока не ответил — обновите статус вручную.");
      return;
    }
    let next: Delivery | null = null;
    try {
      next = await api.getDelivery(sessionId);
    } catch {
      next = null; // сеть моргнула — следующий тик попробует снова
    }
    if (!alive.current) return;
    if (next) {
      setLiveDelivery(next);
      if (next.status === "registered" || next.status === "rejected") {
        setPolling(false);
        await onRefresh().catch(() => null);
        toast(`Передача завершена: ${DELIVERY_LABELS[next.status]}.`);
        return;
      }
      if (next.status === "failed") {
        setPolling(false);
        setNote("Передача не удалась — повторите отправку или откройте ошибку РЭМД.");
        return;
      }
    }
    pollTimer.current = window.setTimeout(() => void deliveryTick(), DELIVERY_POLL_MS);
  };

  const startDeliveryPoll = () => {
    pollDeadline.current = Date.now() + DELIVERY_POLL_LIMIT_MS;
    setPolling(true);
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    pollTimer.current = window.setTimeout(() => void deliveryTick(), DELIVERY_POLL_MS);
  };

  const submit = async () => {
    setBusy("submit");
    setErr("");
    setNote("");
    try {
      const next = await api.submitEmias(sessionId);
      setLiveDelivery(next);
      await onRefresh().catch(() => null);
      if (next.status === "registered") toast("Документ зарегистрирован в РЭМД.");
      else if (next.status === "rejected") toast("РЭМД отклонил документ — смотрите код ошибки.");
      else if (next.status === "failed") {
        setNote("Передача не удалась — повторите отправку или откройте ошибку РЭМД.");
      } else startDeliveryPoll();
    } catch (e: unknown) {
      setErr(documentError(e, "Не удалось передать документ в ЕМИАС"));
    } finally {
      setBusy("");
    }
  };

  const refreshDelivery = async () => {
    setBusy("status");
    setErr("");
    try {
      const next = await api.getDelivery(sessionId);
      setLiveDelivery(next);
      await onRefresh().catch(() => null);
      if (next.status !== "queued" && next.status !== "sending") {
        toast(`Статус передачи: ${DELIVERY_LABELS[next.status]}.`);
      }
    } catch (e: unknown) {
      setErr(documentError(e, "Не удалось получить статус передачи"));
    } finally {
      setBusy("");
    }
  };

  const steps = [
    { label: "Черновик протокола", done: protocol !== null, hint: "заполняет врач" },
    {
      label: "Подтверждён врачом",
      done: protocol !== null && ["approved", "signed", "submitted"].includes(protocol.status),
      hint: "кнопка «Подтвердить протокол» выше",
    },
    { label: "Подписан УКЭП (врач + МО)", done: signed, hint: "две подписи по §7" },
    {
      label: "Передан в ЕМИАС/РЭМД",
      done: protocol?.status === "submitted" || current?.status === "registered",
      hint: "структурированный СЭМД, PDF РЭМД не принимает",
    },
  ];

  const missing = steps.find((step) => !step.done);

  return (
    <details className="card" open>
      <summary className="collapse-summary">Документ</summary>

      {missing ? (
        <p className="hint">
          Следующий шаг — «{missing.label}»: {missing.hint}. Кнопки появляются по мере готовности
          версии протокола, чтобы не подписывать неподтверждённый документ.
        </p>
      ) : (
        <p className="hint">Документ подписан и передан — печатная форма и квитанции доступны.</p>
      )}

      <ul className="doc-steps">
        {steps.map((step) => (
          <li key={step.label}>
            <span className={`badge ${step.done ? "GREEN" : "YELLOW"}`}>
              {step.done ? "готово" : "не готово"}
            </span>{" "}
            {step.label}
          </li>
        ))}
      </ul>

      <div className="row" style={{ marginTop: 10 }}>
        <button
          type="button"
          onClick={() => void sign()}
          disabled={protocol?.status !== "approved" || signed || busy !== ""}
        >
          {busy === "sign" ? "Подписание…" : "Подписать"}
        </button>
        <button type="button" onClick={() => void verify()} disabled={!signed || busy !== ""}>
          {busy === "verify" ? "Проверка…" : "Проверить подпись"}
        </button>
        <button type="button" onClick={() => void downloadPdf()} disabled={!signed || busy !== ""}>
          {busy === "pdf" ? "Формирование…" : "Скачать PDF"}
        </button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!signed || terminal || polling || busy !== ""}
        >
          {busy === "submit" ? "Отправка…" : current?.status === "failed" ? "Повторить передачу" : "Передать в ЕМИАС"}
        </button>
        {(polling || (current && (current.status === "queued" || current.status === "sending"))) && (
          <button type="button" className="ghost" onClick={() => void refreshDelivery()} disabled={busy !== ""}>
            Обновить статус
          </button>
        )}
      </div>

      {!signed && protocol && (
        <p className="small muted">
          Подпись — только для подтверждённой версии (сейчас {protocol.status === "approved" ? "подтверждена" : protocol.status}).
        </p>
      )}

      {signed && signature && (
        <>
          <p className="small muted">
            Подписано {when(signature.signed_at)} · версия {protocol?.version ?? "—"}
          </p>
          {signature.signatures.map((item) => (
            <SignatureBlock key={item.id} signature={item} />
          ))}
          <p className="hint">Демонстрационная подпись: УКЭП аккредитованного УЦ не выдавалась.</p>
        </>
      )}

      {verifyShown && (
        <div style={{ marginTop: 10 }}>
          <p>
            <span className={`badge ${VERIFY_TONE[verifyShown.status]}`}>
              {VERIFY_LABELS[verifyShown.status]}
            </span>{" "}
            <span className="small muted">
              {verifyShown.reason ? `Причина: ${verifyShown.reason}` : `хэш ${verifyShown.document.digest_algorithm}`}
            </span>
          </p>
          <ul className="small muted">
            {verifyShown.signatures.map((item) => (
              <li key={item.id}>
                {ROLE_LABELS[item.role] ?? item.role}: {VERIFY_LABELS[item.status]}
                {item.reason ? ` — ${item.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {current && (
        <div style={{ marginTop: 10 }}>
          <p>
            <span className={`badge ${DELIVERY_TONE[current.status]}`}>
              {DELIVERY_LABELS[current.status]}
            </span>{" "}
            <span className="small muted">
              попыток: {current.attempts}
              {current.request_id ? ` · запрос ${current.request_id.slice(0, 8)}…` : ""}
              {current.status === "registered" ? ` · зарегистрирован ${when(current.registered_at)}` : ""}
            </span>
          </p>
          {current.last_error_code && (
            <p className="error">
              {DELIVERY_ERRORS[current.last_error_code] ?? current.last_error_message ?? "ошибка РЭМД"}
            </p>
          )}
          {current.status === "failed" && (
            <p className="small muted">
              Повтор идёт тем же идентификатором документа — дубль в РЭМД не появится.
            </p>
          )}
        </div>
      )}

      {polling && <p className="small muted">Ожидаем ответ РЭМД (опрос каждые 2 с)…</p>}
      {err && (
        <p className="error" role="alert">
          {err}
        </p>
      )}
      {note && !err && <p className="small muted">{note}</p>}
    </details>
  );
}
