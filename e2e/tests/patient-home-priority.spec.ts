// FR-45/FR-46: главный экран пациента — напоминания первыми и с переходом на нужный
// экран, баннер скрининга ниже; приём лекарств спрашивается в таблетках.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  DEMO_PASSWORD,
  freshPatientEmail,
  login,
  registerPatient,
  setupClinicianPair,
  totp,
} from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: { json: () => Promise<unknown> }) => (await r.json()) as Record<string, unknown>;

/** Свежий пациент после согласия: главный экран уже открыт. */
async function consentAndHome(page: Page) {
  await registerPatient(page, freshPatientEmail("home"));
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();
}

/** Пара «врач ↔ пациент» с назначенным препаратом и (по умолчанию) шкалой PHQ-9. */
async function pairWithPrescription(
  request: APIRequestContext,
  timesPerDay: number,
  scale = "PHQ-9",
) {
  const { docReg, patReg } = await setupClinicianPair(request, "home");
  const doc = await j(
    await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: docReg.email, password: DEMO_PASSWORD, otp: totp(docReg.totp_secret as string) },
    }),
  );
  const auth = { Authorization: `Bearer ${doc.access_token as string}` };
  const prescribe = await request.put(
    `${BASE}/api/v1/clinician/patients/${patReg.id}/prescribed-scales`,
    { data: { scales: [scale] }, headers: auth },
  );
  expect(prescribe.status()).toBe(200);
  const med = await request.post(`${BASE}/api/v1/clinician/patients/${patReg.id}/medications`, {
    headers: auth,
    data: {
      title: `E2E-таблетки-${Date.now()}`,
      dosage: "1 таблетка",
      // Окно приёма считается по московским датам — назначаем с сегодня.
      started_on: new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow" }).format(new Date()),
      times_per_day: timesPerDay,
    },
  });
  expect(med.status()).toBe(201);
  const created = await j(med);
  return { email: patReg.email as string, medId: created.id as string };
}

test.describe("Главный экран пациента: приоритет и переходы", () => {
  test("напоминания выше баннера скрининга, кнопки ≥44px на 390×844", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await consentAndHome(page);

    const checkin = page.getByRole("button", { name: "Отметить чек-ин" });
    const screening = page.getByRole("button", { name: "Перейти к скринингу" });
    await expect(checkin).toBeVisible();
    await expect(screening).toBeVisible();
    // Без активных назначений напоминания о приёме нет — экран ведёт себя как раньше.
    await expect(page.getByRole("button", { name: "Отметить приём" })).toHaveCount(0);

    for (const button of [checkin, screening]) {
      const box = await button.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    }

    // Порядок в документе: список напоминаний целиком выше баннера скрининга.
    const reminders = await page.locator(".patient-reminders").boundingBox();
    const banner = await page.getByText(/Врач назначил скрининг/).boundingBox();
    expect(reminders && banner).toBeTruthy();
    expect(banner!.y).toBeGreaterThanOrEqual(reminders!.y + reminders!.height - 1);
    expect(await page.getByRole("heading", { name: "Напоминания" }).count()).toBe(1);
  });

  test("каждое напоминание открывает свой экран", async ({ page, request }) => {
    const { email } = await pairWithPrescription(request, 1);
    await login(page, email, DEMO_PASSWORD);
    await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();

    // Чек-ин — блок на «Сегодня», фокус сразу на кнопке старта.
    await page.getByRole("button", { name: "Отметить чек-ин" }).click();
    await expect(page.locator("#today-checkin")).toBeInViewport();
    await expect(page.getByRole("button", { name: "Начать чек-ин" })).toBeFocused();

    // Приём лекарств — блок на «Сегодня», фокус на первом варианте ответа.
    await page.getByRole("button", { name: "Отметить приём" }).click();
    await expect(page.locator("#today-intake")).toBeInViewport();
    await expect(page.getByRole("button", { name: /^Принял\(а\) всё/ })).toBeFocused();

    // Скрининг — отдельный экран.
    await page.getByRole("button", { name: "Перейти к скринингу" }).click();
    await expect(page.getByText("Шкала назначена вашим врачом")).toBeVisible();
  });

  test("вопрос про таблетки, отметки taken/partial/missed сохраняются как выбрано", async ({
    page,
    request,
  }) => {
    const { email, medId } = await pairWithPrescription(request, 3);
    await login(page, email, DEMO_PASSWORD);

    await expect(page.getByText("Сколько таблеток вы приняли?")).toBeVisible();
    await expect(page.getByRole("button", { name: "Врач не назначал на сегодня" })).toBeVisible();

    // taken
    await page.getByRole("button", { name: "Принял(а) всё: 3 таблетки" }).click();
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText("Отмечено 1 из 3 приёмов за сегодня")).toBeVisible();

    // partial → уточнение в таблетках
    await page.getByRole("button", { name: "Принял(а) меньше" }).click();
    await page.getByRole("button", { name: "Половину таблетки" }).click();
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText("Отмечено 2 из 3 приёмов за сегодня")).toBeVisible();

    // missed — день закрыт
    await page.getByRole("button", { name: "Не принимал(а)" }).click();
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText("Сегодня всё отмечено: 3 из 3 приёмов")).toBeVisible();
    await expect(page.getByText(/Половину таблетки/)).toBeVisible();

    // API: ровно те отметки, что выбрал пациент (partial_kind не подменяется).
    const pat = await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email, password: DEMO_PASSWORD },
      }),
    );
    const summary = await j(
      await request.get(`${BASE}/api/v1/me/adherence`, {
        headers: { Authorization: `Bearer ${pat.access_token as string}` },
      }),
    );
    // Порядок в выдаче не важен: у каждого статуса свой набор значений.
    type Mark = { medication_id: string | null; status: string; partial_kind: string | null };
    const marks = (summary.items as Mark[])
      .filter((r) => r.medication_id === medId)
      .map((r) => `${r.status}:${r.partial_kind ?? ""}`)
      .sort();
    expect(marks).toEqual(["missed:", "partial:half", "taken:"]);
  });
});
