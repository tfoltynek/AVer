const handlePercentageInputChange = () => {
  const input = document.querySelector(".Form-percentageField-slider input");
  const value = document.querySelector(".Form-percentageField-value");
  value.value = input.value;

  value.textContent = input.value;
  input.addEventListener("input", (event) => {
    value.value = event.target.value;
  });

  value.addEventListener("change", (event) => {
    input.value = event.target.value;
  });
};

handlePercentageInputChange();
