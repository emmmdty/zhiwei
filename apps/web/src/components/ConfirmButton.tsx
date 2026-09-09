// S10-T1：两段式确认按钮（dangerous mutation 的 arm → confirm 模式）。
// 既有视图（eval resume/seal、member removal）各自内联了同一模式——收敛为
// 原语后 DOM 语义不变：label 常驻，armed 后 confirmLabel（可选 notice 文本）
// 出现；confirm 触发 onConfirm 并解除 armed。

import { useState } from "react";

interface ConfirmButtonProps {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  // 主按钮禁用（角色/状态门禁由调用方声明，与既有语义逐字对齐）
  disabled?: boolean;
  // 确认按钮禁用（动作在途）
  confirmDisabled?: boolean;
  // armed 态下的附加提示文本（如 "Confirm removal?"）
  notice?: string;
}

export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
  confirmDisabled,
  notice,
}: ConfirmButtonProps) {
  const [armed, setArmed] = useState(false);
  return (
    <>
      <button onClick={() => setArmed(true)} disabled={disabled}>
        {label}
      </button>
      {armed && (
        <>
          {notice && <span>{notice}{" "}</span>}
          <button
            onClick={() => {
              setArmed(false);
              onConfirm();
            }}
            disabled={confirmDisabled}
          >
            {confirmLabel}
          </button>
        </>
      )}
    </>
  );
}
