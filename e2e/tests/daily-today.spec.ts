// Единый ежедневный экран «Сегодня»: пошаговый адаптивный чек-ин (docs/16), дневник
// с 5-точечной шкалой настроения и отметка о приёме лекарств.
import { expect, test, type Page } from "@playwright/test";
import {
  DEMO_PASSWORD,
  DEMO_TOTP,
  freshPatientEmail,
  login,
  registerPatient,
  totp,
} from "./helpers";

// Ядро чек-ина v2.0.0: MO1, TR1, SL1, IN1, EN1, FU1 (docs/16 §1). HR4 в ядро не входит
// и задаётся отдельным шагом безопасности только когда сработал safety-гейт.
const CORE_ITEMS = 6;
// Индекс кнопки «Сильно» на шкале 0..4 — им включается гейт (MO1 >= 3).
const GATE_SEVERITY = 3;

async function openToday(page: Page) {
  await registerPatient(page, freshPatientEmail());
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();
}

/** Пункты текущего шага отрисованы и шаг можно отправлять (кнопка активна). */
async function waitForStepSettled(page: Page) {
  await expect
    .poll(
      async () => {
        if (await page.getByText("Чек-ин за сегодня выполнен").isVisible()) return "done";
        const next = page
          .getByRole("button", { name: "Далее" })
          .or(page.getByRole("button", { name: "Отправить" }));
        if ((await next.count()) === 0) return "busy";
        return (await next.isDisabled()) ? "ready" : "busy";
      },
      { timeout: 20_000 },
    )
    .not.toBe("busy");
}

/**
 * Отвечает на все пункты текущего шага первой кнопкой (значение 0). `mo1` позволяет
 * поднять MO1 до нужной выраженности: значение ≥ 3 включает safety-гейт.
 */
async function answerStep(page: Page, opts: { mo1?: number } = {}) {
  const questions = page.locator(".qa");
  const count = await questions.count();
  expect(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i++) {
    const question = questions.nth(i);
    const item = await question.getAttribute("data-item");
    const index = item === "MO1" && opts.mo1 !== undefined ? opts.mo1 : 0;
    await question.getByRole("button").nth(index).click();
  }
}

async function nextStep(page: Page) {
  await page.getByRole("button", { name: /^(Далее|Отправить)$/ }).click();
}

/** Проходит чек-ин до конца: ядро → (безопасность | модули) → «выполнен». */
async function completeCheckin(page: Page, yesGate = false) {
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  for (let step = 0; step < 6; step++) {
    await waitForStepSettled(page);
    if (await page.getByText("Чек-ин за сегодня выполнен").isVisible()) return;
    await answerStep(page, yesGate && step === 0);
    await nextStep(page);
  }
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
}

test("«Сегодня»: первый шаг — 7 пунктов ядра; неполный чек-ин не блокирует дневник и приём", async ({
  page,
}) => {
  await openToday(page);
  await expect(page.getByRole("heading", { name: "Чек-ин" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Дневник" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Приём лекарств" })).toBeVisible();

  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await expect(page.locator(".qa")).toHaveCount(CORE_ITEMS);
  
  // Отвечен только один пункт шага — «Далее» заблокирована.
  await page.locator(".qa").first().getByRole("button").first().click();
  await expect(page.getByRole("button", { name: "Далее" })).toBeDisabled();

  // Дневник: настроение — 5 кнопок 0..4, сон — часы.
  const content = `День прошёл ровно ${Date.now()}`;
  await page.locator("#today-diary").fill(content);
  await expect(page.locator(".mood").getByRole("button")).toHaveCount(5);
  // FR-33 F: эмоция — переключатель: выбрана → серым (aria-pressed), повторный клик снимает.
  const excellent = page.locator(".mood").getByRole("button", { name: /Отлично/ });
  await excellent.click();
  await expect(excellent).toHaveAttribute("aria-pressed", "true");
  await excellent.click();
  await expect(excellent).toHaveAttribute("aria-pressed", "false");
  await excellent.click();
  await expect(excellent).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByPlaceholder("Препарат (напр. Авиандр) — необязательно").fill("Авиандр");
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();

  // Неполный чек-ин не мешает сохранить дневник и приём лекарств.
  await expect(page.getByText("Сохранено: дневник · приём лекарств")).toBeVisible();
  // Выбор эмоции не сбрасывается сохранением — пользователь видит, что отправил.
  await expect(excellent).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText(content)).toBeVisible();
  await expect(page.locator(".msg", { hasText: content }).getByText("😄 Отлично")).toBeVisible();
  // Режим не задан → одна отметка в день; в блоке «за сегодня» видно и счётчик,
  // и что именно отмечено.
  await expect(page.getByText("За сегодня отмечено 1 из 1 приёма")).toBeVisible();
  await expect(page.getByText(/Принял \(Авиандр\)/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Далее" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("button", { name: "Далее" })).toBeVisible();
  await expect(page.getByText(content)).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});

/**
 * FR-27: приёмов в день может быть несколько — второй отмечается там же, на
 * «Сегодня», отдельной записью. Назначение «2 раза в день» выдаёт врач:
 * демо-пациент p3 закреплён за doc@compass.demo сидом.
 */
test("2 приёма в день: обе отметки за сегодня сохраняются отдельными записями", async ({
  page,
  request,
}) => {
  const loginApi = async (email: string, otp?: string) => {
    const r = await request.post("/api/v1/auth/login", {
      data: { email, password: DEMO_PASSWORD, otp },
    });
    expect(r.status()).toBe(200);
    return { Authorization: `Bearer ${(await r.json()).access_token as string}` };
  };

  const title = `E2E-дважды-${Date.now()}`;
  const doc = await loginApi("doc@compass.demo", totp(DEMO_TOTP));
  const roster = await (await request.get("/api/v1/clinician/patients", { headers: doc })).json();
  const patientId: string | undefined = roster.items.find(
    (p: { email: string }) => p.email === "p3@compass.demo",
  )?.patient_id;
  expect(patientId).toBeTruthy();

  // Прошлые прогоны оставляют назначения — убираем их, чтобы демо-карта не пухла.
  const medsUrl = `/api/v1/clinician/patients/${patientId}/medications`;
  const existing = await (await request.get(medsUrl, { headers: doc })).json();
  for (const med of existing.items.filter((m: { title: string }) => m.title.startsWith("E2E-дважды-"))) {
    await request.delete(`${medsUrl}/${med.id}`, { headers: doc });
  }

  const created = await request.post(medsUrl, {
    headers: doc,
    data: {
      title,
      dosage: "1 таблетка",
      // Окно приверженности считается по московским датам — назначаем с сегодня.
      started_on: new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow" }).format(new Date()),
      times_per_day: 2,
    },
  });
  expect(created.status()).toBe(201);
  const medId: string = (await created.json()).id;

  await login(page, "p3@compass.demo", DEMO_PASSWORD);
  await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();
  await page.locator("select").selectOption({ label: `${title} (2 раза в день)` });

  // Первый приём: форма остаётся на экране — второй приём ещё не отмечен.
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
  await expect(page.getByText("Сохранено: приём лекарств")).toBeVisible();
  await expect(page.getByText("Отмечено 1 из 2 приёмов за сегодня")).toBeVisible();
  await expect(page.getByRole("button", { name: "Принял", exact: true })).toBeVisible();

  // Второй приём — новая запись; первая не перезаписывается.
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
  await expect(page.getByText("За сегодня отмечено 2 из 2 приёмов")).toBeVisible();
  await expect(page.getByRole("button", { name: "Принял", exact: true })).toHaveCount(0);
  // История приёма остаётся доступной: просмотр и исправление не блокируются.
  await expect(page.getByRole("button", { name: "История приёма" })).toBeVisible();

  await page.reload();
  await expect(page.getByText("За сегодня отмечено 2 из 2 приёмов")).toBeVisible();

  // В БД — две отдельные отметки по этому назначению.
  const patient = await loginApi("p3@compass.demo");
  const summary = await (await request.get("/api/v1/me/adherence", { headers: patient })).json();
  const marks = summary.items.filter((r: { medication_id: string | null }) => r.medication_id === medId);
  expect(marks.map((r: { status: string }) => r.status)).toEqual(["taken", "taken"]);
});

test("полный чек-ин: шаги до «Чек-ин за сегодня выполнен», состояние переживает перезагрузку", async ({
  page,
}) => {
  await openToday(page);
  await completeCheckin(page, false);

  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  // Гейт не сработал → safety=unassessed: день закрыт без вопроса о риске, и текст
  // честно говорит «риск не оценивался», а не «риска нет».
  await expect(page.getByText(/Вопросы о риске сегодня не задавались/)).toBeVisible();
  await expect(page.getByText(/Закреплённого врача у вас нет/)).toHaveCount(0);
  await expect(page.getByText(/видит врач/)).toHaveCount(0);
  await expect(page.locator(".qa")).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});

test("gate-пункт HR4 «Да» ведёт на шаг безопасности, RS3 = «Нет» — экстренный блок", async ({ page }) => {
  await openToday(page);
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await waitForStepSettled(page);
  // Ядро: MO1 = «Сильно» включает safety-гейт, остальное — «Совсем нет».
  await expect(page.locator(".qa")).toHaveCount(CORE_ITEMS);
  await expect(page.locator('[data-item="HR4"]')).toHaveCount(0);
  await answerStep(page, { mo1: GATE_SEVERITY });
  await nextStep(page);

  // Шаг безопасности: единственный пункт HR4 (мысли о самоповреждении / причинении себе вреда).
  await expect(page.locator(".qa")).toHaveCount(1);
  await page.locator('[data-item="HR4"]').getByRole("button").nth(1).click();
  await nextStep(page);

  // Модуль безопасности RS1–RS3, симптоматические модули в этот день не предъявляются.
  await expect(page.locator(".qa")).toHaveCount(3);
  await expect(page.locator('[data-item="RS1"]')).toBeVisible();
  await expect(page.locator('[data-item="RS3"]')).toBeVisible();

  // RS1 = «Нет», RS2 = «Нет», RS3 = «Нет» → срочный сигнал.
  await answerStep(page);
  await nextStep(page);

  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  const safety = page.locator(".safety");
  await expect(safety.getByText("Ваша безопасность важнее всего")).toBeVisible();
  await expect(safety.getByText(/обратитесь за экстренной помощью/).first()).toBeVisible();
  // Без закреплённого врача сигнал не обещает просмотр: только сохранение и совет.
  await expect(safety.getByText(/Закреплённого врача у вас нет/)).toBeVisible();
  await expect(safety.getByText(/видит врач/)).toHaveCount(0);

  // Блок безопасности виден и после перезагрузки.
  await page.reload();
  await expect(page.getByText("Ваша безопасность важнее всего")).toBeVisible();
});

test("прогресс чек-ина восстанавливается после перезагрузки", async ({ page }) => {
  await openToday(page);
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await waitForStepSettled(page);
  await answerStep(page, false);
  await nextStep(page);
  await waitForStepSettled(page);

  // Ядро отвечено целиком — следующий шаг и прогресс пришли с сервера.
  await expect(page.getByText(/Прогресс: \d+ из \d+/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);

  await page.reload();
  await expect(page.getByText(/Прогресс: \d+ из \d+/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});
