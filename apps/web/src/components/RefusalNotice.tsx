// S10-T1：{reason, message} 机器可读拒绝面的结构化呈现原语（S9 releases/
// evals 的 detail 形状；S10 Studio 的 validate/CAS 拒绝同型）。api 客户端把
// detail 序列化为 JSON 文本透传——本模块负责安全解析回结构并双行展示，
// 解析失败返回 null（调用方退回普通错误文本，不猜 reason）。

export interface Refusal {
  reason: string;
  message: string;
}

export function parseRefusal(detail: string): Refusal | null {
  try {
    const parsed: unknown = JSON.parse(detail);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      "reason" in parsed &&
      "message" in parsed &&
      typeof (parsed as { reason: unknown }).reason === "string" &&
      typeof (parsed as { message: unknown }).message === "string"
    ) {
      return parsed as Refusal;
    }
    return null;
  } catch {
    return null;
  }
}

export function RefusalNotice({ refusal }: { refusal: Refusal }) {
  return (
    <div role="alert">
      refused: {refusal.reason} — {refusal.message}
    </div>
  );
}
