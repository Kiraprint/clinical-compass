import { useEffect } from "react";

// FR-47: горячие клавиши кабинета врача — одна точка вместо копий в каждом композере.
// Ctrl+Enter / Cmd+Enter отправляет вопрос ассистенту и сообщение пациенту.
export function isSendShortcut(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey?: boolean;
}): boolean {
  return e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.altKey;
}

// FR-47: Escape закрывает открытый оверлей/панель (меню, колокольчик).
export function useEscapeClose(active: boolean, close: () => void) {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, close]);
}
