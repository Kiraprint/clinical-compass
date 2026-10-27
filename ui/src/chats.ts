import type { PatientCard, PatientChat, PatientChatMessage } from "./api";

/** Имя пациента для шапки и списка: ФИО, иначе e-mail, иначе нейтральное «Пациент». */
export function chatName(
  chats: PatientChat[],
  roster: PatientCard[],
  patientId: string | null,
): string {
  if (!patientId) return "Пациент";
  const chat = chats.find((c) => c.patient_id === patientId);
  const patient = roster.find((p) => p.patient_id === patientId);
  return chat?.fio ?? patient?.fio ?? patient?.email ?? "Пациент";
}

/**
 * Строки списка диалогов. Открытый из карточки пациент без переписки получает
 * пустую строку: чат виден сразу, а не после первого сообщения и обновления.
 */
export function chatRows(
  chats: PatientChat[],
  roster: PatientCard[],
  selected: string | null,
): PatientChat[] {
  if (!selected || chats.some((c) => c.patient_id === selected)) return chats;
  const patient = roster.find((p) => p.patient_id === selected);
  if (!patient) return chats;
  return [
    {
      patient_id: selected,
      fio: patient.fio,
      last_at: "",
      last_sender: null,
      preview: "",
      message_count: 0,
    },
    ...chats,
  ];
}

/**
 * Обновление превью после отправки. Если переписки ещё не было в списке,
 * строка добавляется из ответа сервера — без повторного GET списка.
 */
export function applyChatMessage(
  chats: PatientChat[],
  roster: PatientCard[],
  patientId: string,
  message: PatientChatMessage,
): PatientChat[] {
  const exists = chats.some((c) => c.patient_id === patientId);
  if (!exists) {
    const patient = roster.find((p) => p.patient_id === patientId);
    return [
      {
        patient_id: patientId,
        fio: patient?.fio ?? null,
        last_at: message.created_at,
        last_sender: "CLINICIAN",
        preview: message.content.slice(0, 120),
        message_count: 1,
      },
      ...chats,
    ];
  }
  return chats.map((c) =>
    c.patient_id === patientId
      ? {
          ...c,
          last_at: message.created_at,
          last_sender: "CLINICIAN" as const,
          preview: message.content.slice(0, 120),
          message_count: c.message_count + 1,
        }
      : c,
  );
}
