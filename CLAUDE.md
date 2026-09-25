# General rules

- Choose the plain approach over the clever one, in design, code and writing.
- Keep the architecture simple. Include a feature only when it is necessary and directly serves the user's goal.
- Every line of code must be justified. Skip pedantic tests and needless structure, such as a class for every data object or an enum for every constrained string.
- Keep docs and explanations short and easy to scan for people with ADHD.
- Write self-contained comments. Design docs stay out of the repo, so code stands on its own.
- Offer a subagent review after implementing code.

# Writing style

- Avoid vague or intricate words such as "dovetail", "fold", "tapestry", "testament", "embark", "multifaceted", "backstop".
- Prefer simpler words unless a technical term is needed: "fold" becomes "combine", "embark" becomes "start".
- Write short sentences. Split a long one instead of joining it with em-dashes.
- State the point directly instead of contrasting parallelisms such as "It's not just about X, it's about Y".
- Keep what to do and what to avoid in separate paragraphs. A design doc that mixes what is being built with what is being left out is hard to review.
