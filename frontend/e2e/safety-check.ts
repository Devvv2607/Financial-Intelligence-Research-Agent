export async function assertScriptedApi(apiUrl: string): Promise<void> {
  const response = await fetch(`${apiUrl}/ready`);
  const runtime = response.headers.get("x-agent-runtime");
  if (runtime !== "scripted") {
    throw new Error(
      `Refusing browser E2E: ${apiUrl}/ready reported X-Agent-Runtime=${runtime ?? "<missing>"}; expected scripted.`,
    );
  }
}

export default async function globalSetup(): Promise<void> {
  await assertScriptedApi("http://localhost:8011");
}
