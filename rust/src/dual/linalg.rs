use crate::dual::Dual;
use ndarray::Array;

enum Point {
    f64,
    Dual,
}

pub fn dual_tensordot(a: &Array<Point>, b:&Array<Point>) {
    let a_shape = a.shape();
    let b_shape = b.shape();
    let i: u16; let j: u16;
    (i, j) = (a_shape[a_shape.len()-1], b_shape[0]);
    let mut sum;
    for i in 0..(a_shape[a_shape.len()-1) {
        for j in 0..b_shape[0] {
            let sum = 0;

            sum = sum + a[]
        }
    }

}