import { closeToast, useStore } from "../store";

export function Toasts() {
  const { toast: t } = useStore();
  return (
    <div className="toasts" role="status" aria-live="polite">
      {t && (
        <div className="toast" key={t.id}>
          <span>{t.msg}</span>
          {t.undo && <Undo undo={t.undo} />}
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
