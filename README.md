# AVer
Authorship verifier.

AVer (Authorship Verifier) helps teachers check whether the person who submitted a piece of writing actually wrote it — and understands what is in it. Given a document, AVer blanks out a small number of words and phrases from the author's own text and asks the person to fill them back in under supervision. What makes the method work is which words are removed: instead of deleting every n-th word, AVer uses a multilingual language model (mt5-large) to find items that the author is likely to restore correctly while someone who did not write the text is likely to get wrong. Answers are scored by exact match for single words and by semantic similarity for multi-word items, and converted into a probability of authorship using per-class rates calibrated on 6,515 responses collected in user studies in Czech, Slovak and English.

AVer runs as a standalone web application and is integrated into the Masaryk University information system and the Theses.cz plagiarism-detection service. The result is a probabilistic indicator meant to support a conversation with the student about their submitted work, not to serve as standalone evidence in disciplinary proceedings.

Created with support of the Czech Technological Agency, grant. no. TQ01000110

Working prototype available at: https://aver.pef.mendelu.cz/

## Web App

Web application front-end, database, document processing and cloze-test administration.

## Web API

We service that finds suitable blanks in given document.
