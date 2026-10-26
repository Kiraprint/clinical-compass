/**
 * Регрессия чистых хелперов кратности приёма (node:test, без зависимостей):
 * `node --experimental-strip-types --test test/dosage.test.ts`
 * Лежит вне `src` — `tsc` сборки UI тесты не компилирует.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  describeRegimeMismatch,
  formatTimesPerDay,
  parseIntakeFrequency,
  resolveTimesPerDay,
} from "../src/dosage.ts";

test("parseIntakeFrequency: явные конструкции", () => {
  assert.equal(parseIntakeFrequency("2 раза/сут"), 2);
  assert.equal(parseIntakeFrequency("1 раз в день"), 1);
  assert.equal(parseIntakeFrequency("2 р./сут"), 2);
  assert.equal(parseIntakeFrequency("3 раза в сутки"), 3);
  assert.equal(parseIntakeFrequency("по 20 мг 2 раза/сут"), 2);
});

test("parseIntakeFrequency: диапазон даёт нижнюю границу", () => {
  assert.equal(parseIntakeFrequency("1-2 раза в день"), 1);
});

test("parseIntakeFrequency: доза в сутки — не кратность", () => {
  assert.equal(parseIntakeFrequency("50 мг/сут"), null);
  assert.equal(parseIntakeFrequency("максимум 200 мг/сут"), null);
  assert.equal(parseIntakeFrequency("Внутрь по 20 мг"), null);
});

test("parseIntakeFrequency: неоднозначность и выход за диапазон селекта", () => {
  assert.equal(parseIntakeFrequency("1 раз/сут или 2 раза/сут"), null);
  assert.equal(parseIntakeFrequency("8 раз/сут"), null);
});

test("parseIntakeFrequency: пустая строка", () => {
  assert.equal(parseIntakeFrequency(""), null);
});

test("parseIntakeFrequency: реальные тексты справочника ГРЛС", () => {
  assert.equal(
    parseIntakeFrequency(
      "Внутрь по 20 мг 2 раза/сут. Длительность лечения врач устанавливает индивидуально.",
    ),
    2,
  );
  assert.equal(
    parseIntakeFrequency("Внутрь 1 раз/сут утром или вечером независимо от приёма пищи."),
    1,
  );
  assert.equal(
    parseIntakeFrequency("Внутрь 1 раз/сут независимо от приёма пищи. Депрессия: 10 мг/сут."),
    1,
  );
});

test("formatTimesPerDay: русские формы", () => {
  assert.equal(formatTimesPerDay(1), "1 раз в день");
  assert.equal(formatTimesPerDay(2), "2 раза в день");
  assert.equal(formatTimesPerDay(4), "4 раза в день");
  assert.equal(formatTimesPerDay(6), "6 раз в день");
});

test("resolveTimesPerDay: null сбрасывает select в 1, а не наследует прошлый препарат", () => {
  assert.equal(resolveTimesPerDay({ times_per_day: 2 }), 2);
  assert.equal(resolveTimesPerDay({ times_per_day: null }), 1);
});

test("describeRegimeMismatch: старые назначения расходятся с текстом", () => {
  // Назначение до фикса: в дозировке «2 раза/сут», сохранено 1 — приверженность
  // считается по сохранённому, поэтому расхождение должно быть видно врачу.
  assert.equal(describeRegimeMismatch(1, 2), "в дозировке 2, сохранено 1");
  assert.equal(describeRegimeMismatch(null, 2), "в дозировке 2, сохранено —");
  // Совпало или кратности в тексте нет — править нечего, поведение прежнее.
  assert.equal(describeRegimeMismatch(2, 2), null);
  assert.equal(describeRegimeMismatch(1, null), null);
  assert.equal(describeRegimeMismatch(null, null), null);
});
