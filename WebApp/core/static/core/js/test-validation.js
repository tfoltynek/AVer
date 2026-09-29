function registerInputEvents() {
  const inputs = document.querySelectorAll('input[id^="word_"]');
  const lang = document.documentElement.lang.toLowerCase() || "cs";
  const messages = {
    cs: {
      fillWord: "Vyplňte slovo",
      noSpaces: "Můžete vložit pouze jedno slovo bez mezer",
      noPunctuation: "Nesmí obsahovat interpunkci"
    },
    en: {
      fillWord: "Please fill in the word",
      noSpaces: "You can only enter a single word without spaces",
      noPunctuation: "Punctuation is not allowed"
    },
    sk: {
      fillWord: "Vyplňte slovo",
      noSpaces: "Môžete vložiť iba jedno slovo bez medzier",
      noPunctuation: "Interpunkcia nie je povolená"
    },
  };
  const msg = messages[lang] || messages.cs;

  const punctuationRegex = /[!"#$%&()*+,\-./:;<=>?@[\\\]^_`{|}~]/g;

  inputs.forEach((input) => {
    input.addEventListener("blur", function (e) {
      input.setCustomValidity("");
    });

    input.addEventListener("input", function (e) {
      let value = e.target.value;

      if (value.length > 15) {
        input.size = 15;
      } else if (value.length < 8) {
        input.size = 8;
      } else {
        input.size = value.length;
      }

      if (/\s/.test(value)) {
        input.setCustomValidity(msg.noSpaces);
        input.reportValidity();
        value = value.replace(/\s+/g, "");
        input.value = value;
      }
      else if (punctuationRegex.test(value)) {
        input.setCustomValidity(msg.noPunctuation);
        input.reportValidity();
        value = value.replace(punctuationRegex, "");
        input.value = value;
      }
      else if (value === "") {
        input.setCustomValidity(msg.fillWord);
        input.reportValidity();
      }
      else {
        input.setCustomValidity("");
      }
    });

    input.addEventListener("invalid", function (e) {
      if (input.validity.valueMissing) {
        input.setCustomValidity(msg.fillWord);
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", registerInputEvents);

document.body.addEventListener("htmx:afterSettle", function () {
  registerInputEvents();
});
