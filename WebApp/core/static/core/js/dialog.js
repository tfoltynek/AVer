function handleDialog() {
  const dialog = document.querySelector("#dialog");
  if (!dialog) return;
  const closeButtons = document.querySelectorAll("[data-dialog-close]");
  const confirmButton = document.querySelector("#dialog-confirm");

  dialog.showModal();

  closeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      dialog.remove();
    });
  });

  if (confirmButton) {
    confirmButton.addEventListener("click", () => {
      dialog.close();
    });
  }
}

function removeDialog() {
  const dialog = document.querySelector("#dialog");
  if (dialog) {
    dialog.remove();
  }
}
