# Calculate

Use explicit numeric operands, never an expression or code. Example: `{"operation":"multiply","operands":[12,4]}` returns 48.

Operations: add, subtract, multiply, divide, min and max fold left over 1–32 operands. Remainder and power require exactly two; sqrt and abs require exactly one. Each input and intermediate result must be finite with magnitude at most 1e100. Power exponents have magnitude at most 1024. Division/remainder by zero and negative square roots are errors.

Arithmetic uses f64 precision, so it does not provide exact decimal financial accounting or arbitrary-precision integers. Errors contain no partial result. No eval, process execution, network or credentials are involved. Live Rust access policy still authorizes this read-only call.
