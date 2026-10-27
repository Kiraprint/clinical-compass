// FR-38/39: календарь приёмов, ручной протокол приёма и его подтверждение (docs/18 §4.1, §4.4).
import { expect, test, type APIRequestContext, type APIResponse, type Page } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, openSection, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

/** Дата в формате input[type=date] (локальная, без UTC-сдвига). */
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Свежие врач и пациент с согласием и roster: свой врач — свои слоты, спеки не мешают друг другу. */
async function setupPair(request: APIRequestContext) {
  const docReg = await j(
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("schdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }),
  );
  const patReg = await j(
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("schpat"), password: DEMO_PASSWORD, role: "PATIENT" },
    }),
  );
  const patTok = (
    await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      }),
    )
  ).access_token as string;
  await request.post(`${BASE}/api/v1/consent`, {
    data: { version: 1 },
    headers: { Authorization: `Bearer ${patTok}` },
  });
  const admTok = (
    await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp("JBSWY3DPEHPK3PXP") },
      }),
    )
  ).access_token as string;
  await request.post(`${BASE}/api/v1/admin/roster`, {
    data: { patient_id: patReg.id, clinician_id: docReg.id },
    headers: { Authorization: `Bearer ${admTok}` },
  });
  return { docReg, patReg };
}

/** Вход врача в UI (email/пароль/2FA) — обе сцены спека начинаются одинаково. */
async function loginDoctor(page: Page, docReg: { email: string; totp_secret: unknown }) {
  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(docReg.email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
  await page.getByRole("button", { name: "Войти" }).click();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
}

test("врач записывает приём, ведёт протокол и подтверждает его", async ({ page, request }, testInfo) => {
  const { docReg, patReg } = await setupPair(request);
  await loginDoctor(page, docReg);

  // Новая запись: сегодня 10:00, 50 минут.
  const today = ymd(new Date());
  await page.getByLabel("Пациент для записи").selectOption({ label: patReg.email });
  await page.getByLabel("Дата").fill(today);
  await page.getByLabel("Время").fill("10:00");
  await page.getByLabel("Длительность").selectOption("50");
  await page.getByRole("textbox", { name: "Кабинет", exact: true }).fill("Каб. 7");
  await page.getByRole("button", { name: "Записать", exact: true }).click();
  await expect(page.getByText("Запись создана.")).toBeVisible();

  // Запись видна в списке дня.
  const firstRow = page.getByRole("row", { name: /10:00.*50 мин.*Запланирован/ });
  await expect(firstRow).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("schedule-day.png"), fullPage: true });

  // Пересечение у врача — 409 SLOT_CONFLICT, ошибка строкой в форме, не алертом.
  await page.getByLabel("Время").fill("10:30");
  await page.getByRole("button", { name: "Записать", exact: true }).click();
  await expect(page.getByText("У врача уже есть приём в это время")).toBeVisible();

  // Начать приём → панель сессии.
  await page.getByLabel("Время").fill("11:30");
  await firstRow.getByRole("button", { name: "Начать приём" }).click();
  await expect(page.getByRole("heading", { name: /^Приём:/ })).toBeVisible();

  // Пустой черновик не подтверждается: серверный 409 PROTOCOL_NOT_READY — инлайн, не алертом.
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить протокол" }).click();
  await expect(page.getByText(/Не заполнены обязательные разделы/)).toBeVisible();

  // Заполняем обязательные разделы (§5.2) и сохраняем черновик.
  await page.getByLabel("Правовое основание (ИДС)").fill("ИДС № 12 от 2026-05-04");
  await page.getByLabel("Жалобы").fill("Тревога, нарушения сна, снижение работоспособности.");
  await page.getByLabel("Anamnesis morbi").fill("Симптомы нарастают около трёх месяцев.");
  await page.getByLabel("Суицидальный риск (обязательно)").fill("Не выявлен, суицидальных мыслей нет.");
  await page.getByLabel("МКБ-10").fill("F41.1");
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();

  // Подтверждение → протокол блокируется, показывается read-only вид.
  await page.getByRole("button", { name: "Подтвердить протокол" }).click();
  await expect(page.getByText("Протокол подтверждён.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Сохранить черновик" })).toHaveCount(0);
  await expect(page.getByText("Тревога, нарушения сна")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("protocol-approved.png"), fullPage: true });

  // Перезагрузка: подтверждённое состояние сохраняется.
  await page.reload();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
  await page.getByRole("row", { name: /10:00/ }).getByRole("button", { name: "Открыть приём" }).click();
  // Статус в шапке панели — устойчивый признак: заметка ниже может совпасть по
  // тексту с сообщением карточки ИИ и дать strict-mode сразу на два элемента.
  await expect(page.getByText(/протокол подтверждён \(v\d\)/)).toBeVisible();
  await expect(page.getByText("Тревога, нарушения сна")).toBeVisible();
  await expect(page.getByRole("button", { name: "Подтвердить протокол" })).toHaveCount(0);
});

// §6: приём без записи не должен теряться при закрытии панели и перезагрузке,
// а запись — правиться и отменяться из календаря.
test("приём без записи находится после перезагрузки, запись правится и отменяется", async ({
  page,
  request,
}) => {
  const { docReg, patReg } = await setupPair(request);
  await loginDoctor(page, docReg);

  // Приём без записи → панель → «К расписанию» (штатное закрытие, раньше терял сессию).
  await page.getByRole("button", { name: "Начать приём без записи" }).click();
  await page.getByLabel("Пациент без записи").selectOption({ label: patReg.email });
  await page.getByRole("button", { name: "Начать приём сейчас" }).click();
  await expect(page.getByRole("heading", { name: `Приём: ${patReg.email}` })).toBeVisible();
  await page.getByRole("button", { name: "К расписанию" }).click();
  await expect(page.getByRole("heading", { name: "Приёмы без записи" })).toBeVisible();

  // После перезагрузки сессия находится через список активных приёмов (GET …/sessions?status=active).
  await page.reload();
  await openSection(page, "Расписание");
  const walkRow = page.getByRole("row", { name: new RegExp(patReg.email) });
  await expect(walkRow).toHaveCount(1);
  await walkRow.getByRole("button", { name: "Открыть приём" }).click();
  await expect(page.getByRole("heading", { name: `Приём: ${patReg.email}` })).toBeVisible();

  // Повторный «без записи» для того же пациента продолжает начатую сессию, а не создаёт вторую.
  await page.getByRole("button", { name: "К расписанию" }).click();
  await page.getByRole("button", { name: "Начать приём без записи" }).click();
  await page.getByLabel("Пациент без записи").selectOption({ label: patReg.email });
  await page.getByRole("button", { name: "Начать приём сейчас" }).click();
  await expect(page.getByRole("heading", { name: `Приём: ${patReg.email}` })).toBeVisible();

  // Завершение приёма убирает его из активных.
  await page.getByRole("button", { name: "Завершить приём" }).click();
  await expect(page.getByText("Приём завершён, запись добавлена в карточку пациента")).toBeVisible();
  await page.getByRole("button", { name: "К расписанию" }).click();
  await expect(page.getByRole("heading", { name: "Приёмы без записи" })).toHaveCount(0);

  // Запись → правка времени (PATCH) → отмена (PATCH status=cancelled).
  await page.getByLabel("Пациент для записи").selectOption({ label: patReg.email });
  await page.getByLabel("Дата").fill(ymd(new Date()));
  await page.getByLabel("Время").fill("15:00");
  await page.getByRole("button", { name: "Записать", exact: true }).click();
  await expect(page.getByText("Запись создана.")).toBeVisible();
  const row = page.getByRole("row", { name: /15:00.*Запланирован/ });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Изменить" }).click();
  await page.getByLabel("Новое время").fill("16:15");
  await page.getByRole("button", { name: "Сохранить запись" }).click();
  const moved = page.getByRole("row", { name: /16:15.*50 мин.*Запланирован/ });
  await expect(moved).toBeVisible();
  await moved.getByRole("button", { name: "Отменить" }).click();
  await expect(page.getByRole("row", { name: /16:15.*Отменён/ })).toBeVisible();
});
