import { closeToast, useStore } from "../store";

export function Toasts() {
  const { toast } = useStore();
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toast && (
        <div className="toast" key={toast.id}>
          <span>{toast.msg}</span>
          {toast.undo && <Undo undo={toast.undo} />}
        </div>
      )}
    </div>
  );
}

function Undo({ undo }: { undo: () => void }) {
  return (
    <button
      onClick={() => {
        closeToast();
        undo();
      }}
    >
      Undo
    </button>
  );
}
