const demoInputFirst = document.getElementById("demo-input-1");
const demoInputSecond = document.getElementById("demo-input-2");
const successCheck = document.getElementById("success-check");

const toTypeFirst = demoInputFirst.dataset.answer;
const toTypeSecondBad = "přeci";
const toTypeSecond = demoInputSecond.dataset.answer;

const TYPING_SPEED = 300;
const RESTART_TIMEOUT = 8000;

let indexFirst = 0;
let indexSecond = 0;

const nextLetterFirst = () => {
  if (indexFirst === 0) {
    demoInputFirst.classList.add("answer-result__active");
  }
  if (indexFirst <= toTypeFirst.length) {
    demoInputFirst.value = toTypeFirst.substring(0, indexFirst++);
    setTimeout(nextLetterFirst, TYPING_SPEED);
  } else {
    demoInputFirst.classList.add("answer-result__correct");
    demoInputFirst.classList.remove("answer-result__active");
    setTimeout(nextLetterSecondBad, 2000);
  }
};

const nextLetterSecondBad = () => {
  if (indexSecond === 0) {
    demoInputSecond.classList.add("answer-result__active");
  }
  if (indexSecond <= toTypeSecondBad.length) {
    demoInputSecond.value = toTypeSecondBad.substring(0, indexSecond++);

    setTimeout(nextLetterSecondBad, TYPING_SPEED);
  } else {
    demoInputSecond.classList.add("answer-result__incorrect");
    setTimeout(() => {
      indexSecond = 0;
      demoInputSecond.classList.remove("answer-result__incorrect");
      nextLetterSecond();
    }, 1500);
  }
};

const nextLetterSecond = () => {
  if (indexSecond <= toTypeSecond.length) {
    demoInputSecond.value = toTypeSecond.substring(0, indexSecond++);

    setTimeout(nextLetterSecond, TYPING_SPEED);
  } else {
    demoInputSecond.classList.remove("answer-result__active");
    demoInputSecond.classList.add("answer-result__correct");
    successCheck.classList.remove("demo-successCheck--hidden");
    reset();
  }
};

const reset = () => {
  indexFirst = 0;
  indexSecond = 0;
  setTimeout(() => {
    successCheck.classList.add("demo-successCheck--hidden");
    demoInputFirst.classList.remove(
      "answer-result__active",
      "answer-result__correct"
    );
    demoInputFirst.value = "";

    demoInputSecond.classList.remove(
      "answer-result__active",
      "answer-result__correct"
    );
    demoInputSecond.value = "";
    nextLetterFirst();
  }, RESTART_TIMEOUT);
};

setTimeout(nextLetterFirst, 1000);
