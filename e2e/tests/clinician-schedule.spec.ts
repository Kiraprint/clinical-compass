// FR-38/39: календарь приёмов, ручной протокол приёма и его подтверждение (docs/18 §4.1, §4.4).
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, openSection, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

/** Дата в формате input[type=date] (локальная, без UTC-сдвига). */
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

test("врач записывает приём, ведёт протокол и подтверждает его", async ({ page, request }, testInfo) => {
  // API-подготовка: свежие врач и пациент + согласие + roster (свой врач — свои слоты).
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

  // Вход врача в UI.
  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(docReg.email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
  await page.getByRole("button", { name: "Войти" }).click();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();

  // Новая запись: сегодня 10:00, 50 минут.
  const today = ymd(new Date());
  await page.getByLabel("Пациент").selectOption({ label: patReg.email });
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
  await expect(page.getByText("протокол подтверждён")).toBeVisible();
  await expect(page.getByText("Тревога, нарушения сна")).toBeVisible();
  await expect(page.getByRole("button", { name: "Подтвердить протокол" })).toHaveCount(0);
});
