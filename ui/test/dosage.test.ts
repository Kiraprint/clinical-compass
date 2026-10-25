/**
 * Регрессия чистых хелперов подсказки дозировки (node:test, без зависимостей):
 * `node --experimental-strip-types --test test/dosage.test.ts`
 * Лежит вне `src` — `tsc` сборки UI тесты не компилирует.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveTimesPerDay, stripIntakeFrequency } from "../src/dosage.ts";

test("strip: реальные тексты справочника ГРЛС", () => {
  assert.equal(
    stripIntakeFrequency(
      "Внутрь по 20 мг 2 раза/сут. Длительность лечения врач устанавливает индивидуально.",
    ),
    "Внутрь по 20 мг. Длительность лечения врач устанавливает индивидуально.",
  );
  assert.equal(
    stripIntakeFrequency("Внутрь 1 раз/сут утром или вечером независимо от приёма пищи."),
    "Внутрь утром или вечером независимо от приёма пищи.",
  );
  assert.equal(
    stripIntakeFrequency("Внутрь 1 раз/сут независимо от приёма пищи."),
    "Внутрь независимо от приёма пищи.",
  );
});

test("strip: дозы и титрация остаются дословно", () => {
  const text = "Депрессия: 50 мг/сут; при необходимости дозу повышают шагом 50 мг; максимум 200 мг/сут.";
  assert.equal(stripIntakeFrequency(text), text);
});

test("strip: оборот отдельным предложением не оставляет «..»", () => {
  assert.equal(stripIntakeFrequency("Внутрь. 2 раза/сут."), "Внутрь.");
  assert.equal(stripIntakeFrequency("Внутрь по 20 мг. 2 раза/сут. Курс 4 недели."), "Внутрь по 20 мг. Курс 4 недели.");
});

test("strip: союз двух частотных оборотов уходит вместе с ними", () => {
  // Неоднозначная частота: парсер отдаёт null, текст вырезается целиком.
  assert.equal(stripIntakeFrequency("1 раз/сут или 2 раза/сут по 20 мг"), "по 20 мг");
  assert.equal(stripIntakeFrequency("Внутрь 1 раз/сут либо 2 раза/сут по 20 мг"), "Внутрь по 20 мг");
  assert.equal(stripIntakeFrequency("8 раз/сут по 40 мг"), "по 40 мг");
  assert.equal(stripIntakeFrequency("2 раза/сут, внутрь"), "внутрь");
});

test("strip: «или» между альтернативными схемами остаётся", () => {
  // Справа от «или» — другая доза, а не второй частотный оборот: выкинув союз,
  // получили бы «по 20 мг по 10 мг» — две схемы, склеенные в одну инструкцию.
  assert.equal(
    stripIntakeFrequency("Внутрь по 20 мг 2 раза/сут или по 10 мг 1 раз/сут"),
    "Внутрь по 20 мг или по 10 мг",
  );
});

test("strip: пустая строка", () => {
  assert.equal(stripIntakeFrequency(""), "");
});

test("resolveTimesPerDay: null сбрасывает select в 1, а не наследует прошлый препарат", () => {
  assert.equal(resolveTimesPerDay({ times_per_day: 2 }), 2);
  assert.equal(resolveTimesPerDay({ times_per_day: 1 }), 1);
  assert.equal(resolveTimesPerDay({ times_per_day: null }), 1);
});
