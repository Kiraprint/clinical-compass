/**
 * Регрессия «Открыть в чатах»: пациент из карточки сразу виден в списке и
 * в шапке, а первое сообщение добавляет строку без ручного обновления.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PatientCard, PatientChat } from "../src/api.ts";
import { applyChatMessage, chatName, chatRows } from "../src/chats.ts";

const roster = [
  { patient_id: "p1", email: "p1@test.ru", fio: "Иванов Иван" },
  { patient_id: "p2", email: "p2@test.ru", fio: null },
] as PatientCard[];

const existing: PatientChat[] = [
  {
    patient_id: "p2",
    fio: null,
    last_at: "2026-10-27T10:00:00Z",
    last_sender: "PATIENT",
    preview: "старое",
    message_count: 3,
  },
];

test("chatName: ФИО из ростера, пока переписки ещё нет", () => {
  assert.equal(chatName([], roster, "p1"), "Иванов Иван");
  assert.equal(chatName([], roster, "p2"), "p2@test.ru");
  assert.equal(chatName(existing, roster, "p2"), "p2@test.ru");
  assert.equal(chatName([], roster, null), "Пациент");
});

test("chatRows: открытый пациент без переписки сразу в списке", () => {
  const rows = chatRows(existing, roster, "p1");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].patient_id, "p1");
  assert.equal(rows[0].message_count, 0);
  assert.equal(chatRows(existing, roster, "p2"), existing);
});

test("applyChatMessage: первое сообщение добавляет строку без refresh", () => {
  const rows = applyChatMessage([], roster, "p1", {
    id: "m1",
    sender: "CLINICIAN",
    kind: "TEXT",
    content: "Первое сообщение",
    created_at: "2026-10-27T11:00:00Z",
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].fio, "Иванов Иван");
  assert.equal(rows[0].message_count, 1);
  assert.equal(rows[0].preview, "Первое сообщение");
});

test("applyChatMessage: существующая строка обновляется на месте", () => {
  const rows = applyChatMessage(existing, roster, "p2", {
    id: "m2",
    sender: "CLINICIAN",
    kind: "TEXT",
    content: "Ответ",
    created_at: "2026-10-27T12:00:00Z",
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].message_count, 4);
  assert.equal(rows[0].last_at, "2026-10-27T12:00:00Z");
});
