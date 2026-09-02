# Local Gateway First Profit Lock

Angel V2/V3 local gateway first profit lock is aligned to the server rule:

- initial ATR stop remains unchanged before the threshold;
- first lock can arm only after option premium reaches entry + 4% + estimated round-trip charges;
- the old 0.8R shortcut cannot arm the first lock;
- higher R-based trail stages are evaluated only after the first 4% lock has armed.
