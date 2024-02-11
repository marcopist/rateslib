
pub mod dual;
use dual::dual1::{Dual, DualOrF64};
use dual::linalg::pivot_matrix;
use ndarray::{Array1, Array};
use ndarray::{Array2, arr2, s};

fn main() {
    // let d1 = Dual::new(
    //     1.1,
    //     Vec::from_iter((0..1000).map(|x| x.to_string())),
    //     Vec::from_iter((0..1000).map(|x| f64::from(x)))
    // );
    //
    // use std::time::Instant;
    // let now = Instant::now();
    //
    // // Code block to measure.
    // {
    //     for i in 0..100000 {
    //         let y;
    //         y = &d1 + &d1;
    //     }
    // }
    //
    // let elapsed = now.elapsed();
    // println!("Elapsed: {:.2?}", elapsed / 100000);
    let A: Array2<DualOrF64> = arr2(&[
        [DualOrF64::F64(1.),DualOrF64::Dual(Dual::new(2.0, vec![], vec![]))],
        [DualOrF64::F64(4.),DualOrF64::Dual(Dual::new(5.0, vec![], vec![]))],
    ]);
    let a: (usize, DualOrF64) = A.slice(s![.., 0]).iter().enumerate().fold((0, DualOrF64::F64(0.0)), |acc, (i, elem)| {
        if elem.abs() > acc.1 { (i, elem.clone()) } else { acc }
    });
    // let a = [1, 2, 3, 4, 5];
    // let b = a.into_iter().enumerate().fold((0, 0), |s, (i, j)| (s.0 + i, s.1 + i * j));
    // println!("{:?}", b); // Prints 40

    let (x, y) = pivot_matrix(&A);

    println!("{:?}", A);
    println!("{:?}", A.slice(s![.., 0]));
    println!("{:?}", a);

}
