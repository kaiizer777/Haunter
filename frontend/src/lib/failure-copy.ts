/** Feature-language failure copy for non-admin surfaces.
 * Never expose raw model ids, provider names, or allowlist dumps.
 * Maps backend failure_reason / failure_classification strings to
 * user-facing feature language. Unknown reasons get a generic message.
 */
const MODEL_VOCAB_RE =
  /gpt|claude|llama|deepseek|mistral|gemini|nemotron|laguna|\bling\b|mimo|glm|provider|model|allowlist|anthropic|openai|groq|opencode|context_window|base_url/i;

export function isModelVocabulary(text: string): boolean {
  return MODEL_VOCAB_RE.test(text);
}

export function describeFailureForDisplay(
  failureReason: string | null | undefined,
  failureClassification: string | null | undefined,
  isAdmin: boolean
): string | null {
  const raw = failureReason || failureClassification;
  if (!raw) return null;
  if (isAdmin) return raw;
  if (isModelVocabulary(raw)) {
    return "The automatic fix could not be completed. The team has been notified — please retry or contact support.";
  }
  return raw;
}

/** Generic non-admin-safe message for raw error text that may contain model data. */
export function sanitizeErrorForDisplay(
  message: string,
  isAdmin: boolean
): string {
  if (isAdmin) return message;
  if (isModelVocabulary(message)) {
    return "Something went wrong. Please try again or contact support.";
  }
  return message;
}
