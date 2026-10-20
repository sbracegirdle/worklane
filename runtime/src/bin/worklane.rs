fn main() {
    std::process::exit(worklane_runtime::cli::main(
        std::env::args().skip(1).collect(),
    ));
}
