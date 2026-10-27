// --- События кэша GET ---------------------------------------------------------
// Кэш GET живёт в api.ts (TTL, дедуп полётов, сброс на мутациях). Истёкшую
// запись он отдаёт сразу и обновляет в фоне, поэтому экран не знает, когда
// данные на самом деле обновились. После фонового обновления api.ts шлёт
// window-событие, а экран перечитывает данные обычным путём — они уже в кэше,
// так что это не сетевой запрос, а одна перерисовка.
import { useEffect, useRef } from "react";

export const CACHE_UPDATE_EVENT = "cc:cache-update";

export interface CacheUpdateDetail {
  /** Путь запроса, чья запись обновилась (например, `/api/v1/clinician/patients`). */
  path: string;
}

/**
 * Перечитать экран после фонового обновления кэша GET.
 *
 * `prefix` сужает подписку до своих запросов (одного или нескольких) — экран
 * не дёргается на чужие. Колбэк всегда из свежих пропсов: хук держит ссылку,
 * переподписки на каждую перерисовку нет.
 */
export function useCacheUpdate(
  onUpdate: (path: string) => void,
  prefix?: string | string[],
): void {
  const handler = useRef(onUpdate);
  useEffect(() => {
    handler.current = onUpdate;
  }, [onUpdate]);
  // Список префиксов сравниваем по значению: литерал-массив в пропсе не должен
  // переподписывать слушателя на каждую перерисовку.
  const prefixes = prefix === undefined ? [] : Array.isArray(prefix) ? prefix : [prefix];
  const prefixesKey = prefixes.join("\u0000");
  const prefixesRef = useRef(prefixes);
  prefixesRef.current = prefixes;
  useEffect(() => {
    const listener = (e: Event) => {
      const path = (e as CustomEvent<CacheUpdateDetail>).detail?.path ?? "";
      const wanted = prefixesRef.current;
      if (wanted.length > 0 && !wanted.some((p) => path.startsWith(p))) return;
      handler.current(path);
    };
    window.addEventListener(CACHE_UPDATE_EVENT, listener);
    return () => window.removeEventListener(CACHE_UPDATE_EVENT, listener);
  }, [prefixesKey]);
}
