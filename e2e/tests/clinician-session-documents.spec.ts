// FR-39/40/41: документ приёма — подпись, проверка, PDF и передача в ЕМИАС
// (docs/18 §4.3, §7, §8). ИИ-ассистент в стеке CI выключен (`CC_SESSION_AI_ENABLED=0`),
// поэтому проверяется его информационное состояние без мёртвых кнопок.
import { statSync } from "node:fs";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { DEMO_PASSWORD, openSection, setupClinicianPair, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

test("протокол подписывается, проверяется, печатается и регистрируется в РЭМД", async ({
  page,
  request,
}, testInfo) => {
  const { docReg, patReg } = await setupClinicianPair(request, "sdoc");

  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(docReg.email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
  await page.getByRole("button", { name: "Войти" }).click();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();

  // Приём без записи: панель сессии без календарных конфликтов.
  await page.getByRole("button", { name: "Начать приём без записи" }).click();
  await page.getByLabel("Пациент без записи").selectOption(patReg.id);
  await page.getByRole("button", { name: "Начать приём сейчас" }).click();
  await expect(page.getByRole("heading", { name: /^Приём:/ })).toBeVisible();

  // §4.3: ИИ выключен администратором — карточка объясняет это, кнопок записи нет.
  await expect(page.getByText("ИИ-ассистент выключен администратором")).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать запись" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Сформировать черновик протокола" })).toHaveCount(0);

  // §7/§8: до подтверждения документ объясняет, чего не хватает, вместо мёртвых кнопок.
  await expect(page.getByText("Следующий шаг — «Черновик протокола»")).toBeVisible();
  await expect(page.getByRole("button", { name: "Подписать", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Скачать PDF" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("document-unsigned.png"), fullPage: true });

  // Обязательные разделы §5.2 → черновик → подтверждение.
  await page.getByLabel("Правовое основание (ИДС)").fill("ИДС № 41 от 2026-10-26");
  await page.getByLabel("Жалобы").fill("Тревога, нарушения сна, снижение рабочего тонуса.");
  await page.getByLabel("Суицидальный риск (обязательно)").fill("Не выявлен, суицидальных мыслей нет.");
  await page.getByLabel("МКБ-10").fill("F41.1");
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();
  // Регресс P1 ревью #88: ответ сервера не перезаливает редактор — введённый текст цел.
  await expect(page.getByLabel("Жалобы")).toHaveValue(
    "Тревога, нарушения сна, снижение рабочего тонуса.",
  );

  // Черновик сохранён, но не подтверждён: следующий шаг — подтверждение.
  await expect(page.getByText("Следующий шаг — «Подтверждён врачом»")).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить протокол" }).click();
  await expect(page.getByText("Протокол подтверждён.")).toBeVisible();

  // Подтверждённая версия: следующий шаг — подпись, кнопка доступна.
  await expect(page.getByText("Следующий шаг — «Подписан УКЭП (врач + МО)»")).toBeVisible();
  const signButton = page.getByRole("button", { name: "Подписать", exact: true });
  await expect(signButton).toBeEnabled();
  await signButton.click();

  // Успех подписи не должен прятаться за ошибкой: ответ — протокол + сводка подписей.
  await expect(page.getByText(/^Протокол подписан: /)).toBeVisible();
  await expect(page.locator(".error")).toHaveCount(0);

  // Две подписи (§7): врач и МО, у каждой — реквизиты сертификата.
  const blocks = page.locator(".signature-block");
  await expect(blocks).toHaveCount(2);
  await expect(blocks.first()).toContainText("ПОДПИСАНО");
  await expect(blocks.nth(1)).toContainText("Медицинская организация");
  await expect(
    page.getByText("Демонстрационная подпись: УКЭП аккредитованного УЦ не выдавалась."),
  ).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("protocol-signed.png"), fullPage: true });

  // §7.4: проверка подписи — статус-чип «действительна» + хэш ГОСТ.
  await page.getByRole("button", { name: "Проверить подпись" }).click();
  await expect(page.locator(".badge").filter({ hasText: "подпись действительна" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("signature-verified.png"), fullPage: true });

  // §7.5: печатная форма — непустой PDF, имя файла по версии протокола.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Скачать PDF" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^protocol-\d+\.pdf$/);
  const pdfPath = await download.path();
  expect(pdfPath).toBeTruthy();
  expect(statSync(pdfPath as string).size).toBeGreaterThan(1000);
  await download.saveAs(testInfo.outputPath("protocol.pdf"));

  // §8: передача в ЕМИАС — 202, затем опрос доставки до `registered`.
  await page.getByRole("button", { name: "Передать в ЕМИАС" }).click();
  await expect(page.locator(".badge").filter({ hasText: "зарегистрирован в РЭМД" })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByRole("button", { name: "Передать в ЕМИАС" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("emias-registered.png"), fullPage: true });
});

// Регрессы P1 ревью #88: ввод врача не теряется ни при сохранении, ни при генерации.
async function openWalkIn(
  page: Page,
  doctor: { email: string; totp_secret: string | null },
  patientId: string,
) {
  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(doctor.email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByLabel("Код 2FA").fill(totp(doctor.totp_secret as string));
  await page.getByRole("button", { name: "Войти" }).click();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
  await page.getByRole("button", { name: "Начать приём без записи" }).click();
  await page.getByLabel("Пациент без записи").selectOption(patientId);
  await page.getByRole("button", { name: "Начать приём сейчас" }).click();
  await expect(page.getByRole("heading", { name: /^Приём:/ })).toBeVisible();
}

test("текст, допечатанный во время сохранения, не выбрасывается", async ({ page, request }, testInfo) => {
  const { docReg, patReg } = await setupClinicianPair(request, "srace");
  // PUT протокола отвечает через 3 с: врач успевает допечатать в то же поле.
  await page.route("**/api/v1/clinician/sessions/*/protocol", async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 3000));
    return route.fallback();
  });
  await openWalkIn(page, docReg, patReg.id);

  await page.getByLabel("Правовое основание (ИДС)").fill("ИДС № 77 от 2026-10-26");
  await page.getByLabel("Жалобы").fill("первая часть");
  await page.getByLabel("Суицидальный риск (обязательно)").fill("Не выявлен.");
  await page.getByLabel("МКБ-10").fill("F41.1");

  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await page.getByLabel("Жалобы").fill("первая часть ДОПЕЧАТКА");

  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();
  // Серверное эхо не откатывает допечатанное — ни в редакторе, ни в сохранённой версии.
  await expect(page.getByLabel("Жалобы")).toHaveValue("первая часть ДОПЕЧАТКА");
  await page.screenshot({ path: testInfo.outputPath("save-race.png"), fullPage: true });

  // Повторное сохранение отправляет именно этот текст, подтверждение фиксирует его.
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить протокол" }).click();
  await expect(page.getByText("Протокол подтверждён.")).toBeVisible();
  await expect(page.locator(".protocol-view")).toContainText("первая часть ДОПЕЧАТКА");
});

const DRAFT_SECTIONS = {
  passport: { visit_at: "", channel: "", legal_basis: "" },
  complaints: "ЧЕРНОВИК ИИ",
  anamnesis_morbi: "",
  somatic_status: "",
  recommendations: "",
  mental_status: {
    appearance_behavior: "",
    consciousness_orientation: "",
    perception: "",
    thinking_speech: "",
    emotions_volition: "",
    criticism: "",
    suicidal_risk: "",
  },
  diagnosis: { syndrome: "", icd10: "", rationale: "" },
  treatment: [],
};

test("черновик ИИ не перезаписывает текст, введённый во время генерации", async ({
  page,
  request,
}, testInfo) => {
  const { docReg, patReg } = await setupClinicianPair(request, "sdraft");
  // ИИ в стеке CI выключен: пробу доступности и ответ модели подменяем (клиентская логика).
  await page.route("**/api/v1/clinician/sessions/*/transcript", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        // FR-42: расшифровка с ролями — бейдж «Врач», без плашки деградации.
        segments: [
          {
            start: 0,
            end: 12,
            speaker: "A",
            role: "clinician",
            text: "Здравствуйте, что вас беспокоит?",
          },
        ],
        model: "whisper-e2e",
        duration_sec: 12,
        mask_categories: {},
        depersonalized: true,
        diarized: true,
        speaker_roles: { A: "clinician" },
        speaker_roles_assumed: false,
      }),
    }),
  );
  await page.route("**/api/v1/clinician/sessions/*/protocol/draft", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        id: "00000000-0000-4000-8000-000000000000",
        session_id: "00000000-0000-4000-8000-000000000000",
        version: 1,
        status: "draft",
        source: "llm",
        content_sha256: null,
        diagnosis_code: null,
        model: "test",
        prompt_version: "test",
        needs_review: [{ section: "mental_status", reason: "clinical_risk" }],
        created_by: "00000000-0000-4000-8000-000000000000",
        approved_by: null,
        approved_at: null,
        created_at: null,
        sections: DRAFT_SECTIONS,
      }),
    });
  });
  await openWalkIn(page, docReg, patReg.id);

  // Карточка ИИ доступна (транскрипт есть), но свёрнута — раскрываем и берём черновик.
  await page.getByText("ИИ-ассистент").click();
  const draftButton = page.getByRole("button", { name: "Сформировать черновик протокола" });
  await expect(draftButton).toBeEnabled();
  await draftButton.click();

  // Врач начинает заполнять «Жалобы», пока модель думает: этот текст перезаписывать нельзя.
  await page.getByLabel("Жалобы").fill("мой текст");
  await expect(page.getByText(/Черновик ИИ перенесён в редактор|не перезаписаны/)).toBeVisible();
  await expect(page.getByLabel("Жалобы")).toHaveValue("мой текст");
  await expect(page.getByText(/не перезаписаны/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("draft-race.png"), fullPage: true });
});

test("старый API без полей диаризации не роняет карточку ИИ", async ({ page, request }, testInfo) => {
  const { docReg, patReg } = await setupClinicianPair(request, "slegacy");
  // Бандл новее API (раскатка контейнеров по одному): в ответе нет ни `diarized`,
  // ни `speaker_roles` — регресс P1 ревью #95, раньше карточка падала целиком.
  await page.route("**/api/v1/clinician/sessions/*/transcript", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        segments: [{ start: 0, end: 7, speaker: "A", text: "Здравствуйте, что вас беспокоит?" }],
        model: "whisper-legacy",
        duration_sec: 7,
        mask_categories: {},
        depersonalized: true,
      }),
    }),
  );
  await openWalkIn(page, docReg, patReg.id);

  await page.getByText("ИИ-ассистент").click();
  // Карточка жива: расшифровка видна, метка кластера на месте, плашка честная.
  await expect(page.getByText("Здравствуйте, что вас беспокоит?")).toBeVisible();
  await expect(page.getByText("Говорящий A")).toBeVisible();
  await expect(page.getByText("Говорящие не разделены")).toBeVisible();
  await expect(page.getByRole("button", { name: "Сформировать черновик протокола" })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("legacy-transcript.png"), fullPage: true });
});
