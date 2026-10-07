import { closeToast, useStore } from "../store";
import { Icon } from "./Icon";

export function Toasts() {
  const { toast } = useStore();
  return (
    <div role="status" aria-live="polite">
      {toast && (
        <div className="toast" key={toast.id}>
          <span className="msg">{toast.msg}</span>
          {toast.undo && (
            <button
              onClick={() => {
                const undo = toast.undo!;
                closeToast();
                undo();
              }}
            >
              Undo
            </button>
          )}
          <button className="icon-btn" aria-label="Close" onClick={closeToast}>
            <Icon name="close" size="s" />
          </button>
        </div>
      )}
    </div>
  );
}
